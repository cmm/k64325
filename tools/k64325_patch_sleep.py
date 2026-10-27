#!/usr/bin/env python3
"""Prepare the hash-pinned V0114 automatic-sleep2 candidate offline."""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

IMAGE_SIZE = 16384
SOURCE_SHA256 = "c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1"
LOADER_END = 0x0B00
HOOK = 0x2262
HOOK_BEFORE = bytes.fromhex("12 28 45")
HOOK_AFTER = bytes.fromhex("02 2e 00")
INSERT = 0x2E00
INSTRUCTIONS = (
    ("12 28 45", "LCALL 0x2845", "Transmit the displaced configuration data 00/01."),
    ("c0 d0", "PUSH PSW", "Save status after the original transmit."),
    ("7f 85", "MOV R7,#0x85", "Select sensor register 0x05 for writing."),
    ("12 28 45", "LCALL 0x2845", "Transmit the write address."),
    ("7f b0", "MOV R7,#0xb0", "Keep sleep1 enabled; disable automatic sleep2."),
    ("12 28 45", "LCALL 0x2845", "Transmit the operation-mode data."),
    ("d0 d0", "POP PSW", "Restore status from the original transmit."),
    ("02 22 65", "LJMP 0x2265", "Resume before the existing sensor-ready update."),
)
TRAMPOLINE = bytes.fromhex(" ".join(raw for raw, _, _ in INSTRUCTIONS))
WARNINGS = (
    "Disables automatic sleep2 only; sleep1 remains enabled.",
    "The PAW3205DB-TJ3T sensor register map is unconfirmed for this device.",
    (
        "The image retains post-entry boot-marker metadata and is NOT a full-image "
        "flashing prescription."
    ),
    "The backup-only CLI cannot install this candidate.",
    "This candidate is pinned to one tested V0114 image; USB identity alone does not establish compatibility.",
)


class PatchError(Exception):
    """Input or output was refused."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_source(path):
    # Refuse special files before opening, then recheck the opened file.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise PatchError("input must be a regular file, not a symlink or special file")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise PatchError("input is not a regular file")
        image = source.read(IMAGE_SIZE + 1)
    if len(image) != IMAGE_SIZE:
        raise PatchError(f"input must be exactly {IMAGE_SIZE} bytes")
    # These checks precede the hash gate so failures identify the changed area.
    if image[HOOK : HOOK + len(HOOK_BEFORE)] != HOOK_BEFORE:
        raise PatchError("original hook at 0x2262 is not 12 28 45")
    if image[INSERT : INSERT + len(TRAMPOLINE)] != b"\xff" * len(TRAMPOLINE):
        raise PatchError("insertion range [0x2e00,0x2e14) is not all ff")
    if sha256(image) != SOURCE_SHA256:
        raise PatchError(
            "input SHA256 is not the exact supported V0114 dump; no override"
        )
    return image


def prepare(source, source_path):
    candidate = bytearray(source)
    changes = []
    for offset, after, intent in (
        (
            HOOK,
            HOOK_AFTER,
            "Jump from the shared configuration-data call to the trampoline.",
        ),
        (
            INSERT,
            TRAMPOLINE,
            "Complete configuration, write 0x05=0xb0, restore PSW, resume.",
        ),
    ):
        end = offset + len(after)
        changes.append(
            {
                "start": offset,
                "end": end,
                "before": source[offset:end].hex(),
                "after": after.hex(),
                "intent": intent,
            }
        )
        candidate[offset:end] = after
    candidate = bytes(candidate)
    disassembly = []
    offset = INSERT
    for raw, instruction, intent in INSTRUCTIONS:
        code = bytes.fromhex(raw)
        disassembly.append(
            {
                "offset": offset,
                "bytes": code.hex(),
                "instruction": instruction,
                "intent": intent,
            }
        )
        offset += len(code)
    manifest = {
        "format_version": 1,
        "status": "offline_candidate",
        "addressing": "zero-based byte offsets; range ends are exclusive",
        "source": {
            "path": str(source_path),
            "size": len(source),
            "sha256": sha256(source),
        },
        "output": {
            "file": "candidate.bin",
            "size": len(candidate),
            "sha256": sha256(candidate),
        },
        "changes": changes,
        "loader": {
            "start": 0,
            "end": LOADER_END,
            "sha256": sha256(source[:LOADER_END]),
            "preserved": True,
        },
        "metadata": {
            "start": 0x0B00,
            "end": 0x0C00,
            "sha256": sha256(source[0x0B00:0x0C00]),
            "post_entry_marker_offset": 0x0BEA,
            "post_entry_marker_bytes": source[0x0BEA:0x0BEE].hex(),
            "preserved": True,
        },
        "inserted_disassembly": disassembly,
        "sensor_map_assumption": (
            "Unconfirmed PAW3205DB-TJ3T map and post-reset defaults: register 0x05 "
            "defaults to 0xb8; writing 0xb0 clears automatic sleep2 enable bit 3, "
            "retaining sleep1 enable bit 4, LED-shutter bit 7 and reserved bits 6:5=01."
        ),
        "warnings": list(WARNINGS),
    }
    return candidate, manifest


def write_output(directory, candidate, manifest):
    if directory.name in ("", ".", ".."):
        raise PatchError("output must name a new directory")
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(directory.parent, parent_flags)
    try:
        # mkdir refuses existing files, directories and dangling symlinks.
        os.mkdir(directory.name, 0o700, dir_fd=parent_fd)
        dir_fd = os.open(directory.name, parent_flags, dir_fd=parent_fd)
        try:
            files = (
                ("candidate.bin", candidate),
                (
                    "manifest.json",
                    (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
                ),
            )
            for name, data in files:
                flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prepare a hash-pinned V0114 automatic-sleep2 candidate offline. No USB access.",
        epilog="\n".join(f"WARNING: {warning}" for warning in WARNINGS),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="exact V0114 dump; regular non-symlink file",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        metavar="NEW_DIRECTORY",
        help="new directory; parent must exist and not be a symlink",
    )
    args = parser.parse_args(argv)
    try:
        source = load_source(args.input)
        candidate, manifest = prepare(source, args.input)
        write_output(args.out, candidate, manifest)
    except (OSError, PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for warning in WARNINGS:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"offline_candidate: {args.out / 'candidate.bin'}")
    print(f"source SHA256: {manifest['source']['sha256']}")
    print(f"output SHA256: {manifest['output']['sha256']}")
    print(f"manifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
