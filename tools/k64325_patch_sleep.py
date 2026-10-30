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
# Experimental PAW3205 register 0x06 settings; effective CPI is unconfirmed.
CPI_CONFIG_VALUES = {600: 0x00, 800: 0x01, 1000: 0x02, 1200: 0x03, 1600: 0x04}
PROFILE_SHA256 = {
    (10, None): "7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074",
    (4, None): "4169e8eec62913ff43bb8693b1144893575fbf9d93ecbbdc3e1b2716ce007028",
    (2, None): "187c6944854281080d8fc744a916182807dc08037d22bed894de39dff0310d0d",
    (1, None): "188b4d05d99f9a3274bdbbf51203e597eabe286b887338f6d4fd1d6e8fb4e561",
    (10, 600): "1b11802f6141574b5eacfb832ddb121075f06552ef4108729b85f04c677dae5f",
    (4, 600): "41433602b5db00895daade49e9185a90c5285e3549e1c518e6bf7446d8c8b167",
    (2, 600): "9ec0bb704a4f67b56ddc203d44a77a9f1718dd8bd74529d43e62d19214e8c9b8",
    (1, 600): "3cb034e2e678d47c439805e435ad8c4cd9ed6b60a258e45a51522763c1c500db",
    (10, 800): "216593b18aff227b14b5430081983c5add0b130559a088f1fb74d9b1b1cd9c15",
    (4, 800): "9473c2066f24b76e612b85e9966cf835cdcb43f45c7f6eabbb011937d4431520",
    (2, 800): "7c0b7e624ffe908a2b433b01b4c0633c319c98436d288ae85fd8d2b6a725e1fd",
    (1, 800): "3503728739ed915aa9c235baa95ca08989397240e5b5edbebd879cb90365c15d",
    (10, 1000): "365122f44574e4350b7d6aea6722a71577b930819889816899ddc83031703914",
    (4, 1000): "dc0614392fe61dd049fbf57e5144c2c9670ba1e8f0a03c92a8a7cb584ea00d91",
    (2, 1000): "8b3fc3caa4fad8da5e16aa62c446a364fbb8d6fc5bb754144036acc2fcc7da08",
    (1, 1000): "59ed55765da2572353cc634c4b60deddf4afb9ac60dfa4f35c41dac187d9b0aa",
    (10, 1200): "44ff299ca850147c488761aa6a55361780b709826eec04e282b258fb15d9cd85",
    (4, 1200): "2b48798f2e31cf8149da684a5fb6e78e03003d6dfbdf873e19875371937cf845",
    (2, 1200): "f6b8b281380f9ad6f3dd43c784aa7d47ec9ba0ac3bd3cf272c138e9ad07648a8",
    (1, 1200): "c89788a5fcf31e88d97a15b6b155782ce91b5f239f74b35096f35815a16bae93",
    (10, 1600): "2aca3055eb0c4f6a1bcb25a4b7db1eb44747633b29993a773d1d06040150fc57",
    (4, 1600): "1db26ef99454befee78a70ab1cc1af3dc21ac83518af5164ae12d7635885e0fa",
    (2, 1600): "45988f1870ebfaec0d01904b78c0b48a27334de71497174155b4bc248b0f8f02",
    (1, 1600): "f53676d458491f1d586a17edbaf17c946c57b62c127442550f05dadb0d911dd4",
}
POLL_INTERVALS = tuple(dict.fromkeys(interval for interval, _ in PROFILE_SHA256))
CPI_CHOICES = tuple(CPI_CONFIG_VALUES)
APP_USB_START = 0x1F03
POLL_INTERVAL_OFFSET = 0x1F36
LOADER_END = 0x0B00
HOOK = 0x2262
HOOK_BEFORE = bytes.fromhex("12 28 45")
HOOK_AFTER = bytes.fromhex("02 2e 00")
INSERT = 0x2E00
INSTRUCTIONS = (
    ("12 28 45", "LCALL 0x2845", "Transmit the displaced sensor configuration data."),
    ("c0 d0", "PUSH PSW", "Save status after the original transmit."),
    ("7f 85", "MOV R7,#0x85", "Select sensor register 0x05 for writing."),
    ("12 28 45", "LCALL 0x2845", "Transmit the write address."),
    ("7f b0", "MOV R7,#0xb0", "Keep sleep1 enabled; disable automatic sleep2."),
    ("12 28 45", "LCALL 0x2845", "Transmit the operation-mode data."),
    ("d0 d0", "POP PSW", "Restore status from the original transmit."),
    ("02 22 65", "LJMP 0x2265", "Resume before the existing sensor-ready update."),
)
TRAMPOLINE = bytes.fromhex(" ".join(raw for raw, _, _ in INSTRUCTIONS))
POLLING_WARNING = (
    "Shortened bInterval values are experimental and nonstandard for low-speed USB; "
    "the descriptor interval does not establish a measured report rate."
)
_CPI_WARNING_TEMPLATE = (
    "{request} experimental; the sensor variant/register map and "
    "resulting CPI are unconfirmed."
)
CPI_WARNING = _CPI_WARNING_TEMPLATE.format(request="CPI overrides are")
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


def profile_parameters(image):
    if len(image) != IMAGE_SIZE:
        return None
    fingerprint = sha256(image)
    return next(
        (
            parameters
            for parameters, digest in PROFILE_SHA256.items()
            if digest == fingerprint
        ),
        None,
    )


def profile_interval(image):
    parameters = profile_parameters(image)
    return None if parameters is None else parameters[0]


def validate_source(image):
    if not isinstance(image, (bytes, bytearray)) or len(image) != IMAGE_SIZE:
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
    validate_source(image)
    return image


def prepare(source, source_path, poll_interval_ms=10, cpi=None):
    if type(poll_interval_ms) is not int or poll_interval_ms not in POLL_INTERVALS:
        raise PatchError("poll interval must be 10, 4, 2, or 1 ms")
    if cpi is not None and (type(cpi) is not int or cpi not in CPI_CHOICES):
        raise PatchError(
            f"CPI override must be {', '.join(map(str, CPI_CHOICES))}, "
            "or None for stock configuration"
        )
    if (poll_interval_ms, cpi) not in PROFILE_SHA256:
        raise PatchError("unsupported polling/CPI profile")
    validate_source(source)
    candidate = bytearray(source)
    changes = []
    edits = [
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
    ]
    if poll_interval_ms != 10:
        edits.append(
            (
                POLL_INTERVAL_OFFSET,
                bytes((poll_interval_ms,)),
                "Request an experimental, nonstandard low-speed USB bInterval.",
            )
        )
    if cpi is not None:
        config = CPI_CONFIG_VALUES[cpi]
        edits.extend(
            (
                (
                    0x2252,
                    bytes((config,)),
                    f"Select configuration 0x06=0x{config:02x} for ID 0x31 (requested {cpi} CPI).",
                ),
                (
                    0x2260,
                    bytes((0x7F, config)),
                    f"Select configuration 0x06=0x{config:02x} for ID 0x30 (requested {cpi} CPI).",
                ),
            )
        )
    for offset, after, intent in edits:
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
    if sha256(candidate) != PROFILE_SHA256[poll_interval_ms, cpi]:
        raise PatchError("generated image does not match the pinned profile SHA256")
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
        "polling": {
            "bInterval": poll_interval_ms,
            "offset": POLL_INTERVAL_OFFSET,
            "unit": "ms",
            "experimental_low_speed": poll_interval_ms != 10,
        },
        "cpi": {"requested": cpi, "experimental": cpi is not None},
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
        "warnings": list(WARNINGS)
        + ([POLLING_WARNING] if poll_interval_ms != 10 else [])
        + (
            [_CPI_WARNING_TEMPLATE.format(request=f"Requested {cpi} CPI is")]
            if cpi is not None
            else []
        ),
    }
    return candidate, manifest


def write_output(directory, candidate, manifest):
    if directory.name in ("", ".", ".."):
        raise PatchError("output must name a new directory")
    output_file = manifest["output"]["file"]
    if (
        not isinstance(output_file, str)
        or output_file in ("", ".", "..")
        or Path(output_file).name != output_file
        or "\0" in output_file
    ):
        raise PatchError(
            "output file must be a nonempty local basename other than . or .."
        )
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(directory.parent, parent_flags)
    try:
        # mkdir refuses existing files, directories and dangling symlinks.
        os.mkdir(directory.name, 0o700, dir_fd=parent_fd)
        dir_fd = os.open(directory.name, parent_flags, dir_fd=parent_fd)
        try:
            files = (
                (output_file, candidate),
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
        epilog="\n".join(
            f"WARNING: {warning}"
            for warning in (*WARNINGS, POLLING_WARNING, CPI_WARNING)
        ),
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
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        choices=POLL_INTERVALS,
        default=10,
        help="USB descriptor bInterval in ms (default: 10); 4/2/1 are experimental and nonstandard for low-speed USB",
    )
    parser.add_argument(
        "--cpi",
        type=int,
        choices=CPI_CHOICES,
        help="requested experimental CPI override; omit to keep stock ID-dependent configuration",
    )
    args = parser.parse_args(argv)
    try:
        source = load_source(args.input)
        candidate, manifest = prepare(
            source, args.input, args.poll_interval_ms, args.cpi
        )
        write_output(args.out, candidate, manifest)
    except (OSError, PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for warning in manifest["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"offline_candidate: {args.out / 'candidate.bin'}")
    print(f"source SHA256: {manifest['source']['sha256']}")
    print(f"output SHA256: {manifest['output']['sha256']}")
    print(f"USB descriptor bInterval: {args.poll_interval_ms} ms")
    if args.cpi is not None:
        print(f"Requested CPI: {args.cpi} (experimental; unconfirmed)")
    print(f"manifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
