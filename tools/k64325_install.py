#!/usr/bin/env python3
"""Install or restore hash-pinned V0114 sleep/polling application images."""

import argparse
import json
import os
import shlex
import stat
import sys
import time
from contextlib import ExitStack
from functools import partial
from pathlib import Path

import k64325_backup as backup
import k64325_patch_sleep as patch

ORIGINAL_SHA256 = patch.SOURCE_SHA256
PAGE_SIZE = 256
ALLOWED_PAGES = (0x1F00, 0x2200, 0x2E00)
PAGE_ORDER = {
    "install": (0x2E00, 0x2200, 0x1F00),
    "rollback": (0x2200, 0x2E00, 0x1F00),
}
WARNING = (
    "If flashing fails, keep the device connected in loader mode and inspect it "
    "before retrying or exiting."
)


def encode_erase(page):
    if type(page) is not int or page not in ALLOWED_PAGES:
        raise backup.BackupError("erase requires page 0x1f00, 0x2200, or 0x2e00")
    return bytes((1, 1, 1, (page >> 8) - 0x0C)) + bytes(60)


def encode_program(address, data):
    if type(address) is not int or not isinstance(data, bytes):
        raise backup.BackupError("program requires an integer address and bytes")
    count = len(data)
    page = address & ~(PAGE_SIZE - 1)
    if (
        page not in ALLOWED_PAGES
        or address % 2
        or not 2 <= count <= 58
        or count % 2
        or address + count > page + PAGE_SIZE
    ):
        raise backup.BackupError(
            "program requires an even address and even length 2..58 within one "
            "allowed page (0x1f00, 0x2200, or 0x2e00)"
        )
    return bytes((1, 2, count, address >> 8, address & 255)) + data + bytes(59 - count)


def validate_write_packet(packet):
    """Recheck complete packets at the transport boundary, including padding."""
    if not isinstance(packet, bytes) or len(packet) != 64 or packet[0] != 1:
        raise backup.BackupError("write packet must be a 64-byte ID-1 report")
    if packet[1] == 1:
        address = (packet[3] + 0x0C) << 8
        canonical = encode_erase(address)
        command = "erase"
    elif packet[1] == 2:
        address = int.from_bytes(packet[3:5], "big")
        canonical = encode_program(address, packet[5 : 5 + packet[2]])
        command = "program"
    else:
        raise backup.BackupError("only erase/program packets may use the write path")
    if packet != canonical:
        raise backup.BackupError("noncanonical write packet or nonzero padding")
    return command, address


class Transaction:
    """Exclusive output directory and fsynced, append-only intent/ACK journal."""

    def __init__(self, directory, header):
        self.directory = directory
        self.header = header
        self.stack = ExitStack()
        self.sequence = 0
        self.packet_id = 0

    def keep_fd(self, fd):
        self.stack.callback(os.close, fd)
        return fd

    def __enter__(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            parent_fd = self.keep_fd(os.open(self.directory.parent, flags))
            if self.directory.name in ("", ".", ".."):
                raise backup.BackupError("output must name a new directory")
            os.mkdir(self.directory.name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            self.dir_fd = self.keep_fd(
                os.open(self.directory.name, flags, dir_fd=parent_fd)
            )
            os.fchmod(self.dir_fd, 0o700)
            self.journal_fd = self.keep_fd(
                os.open(
                    "journal.jsonl",
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC
                    | os.O_APPEND,
                    0o600,
                    dir_fd=self.dir_fd,
                )
            )
            os.fchmod(self.journal_fd, 0o600)
            os.fsync(self.dir_fd)
            self.append("header", format_version=1, **self.header)
        except BaseException:
            self.stack.close()
            raise
        return self

    def append(self, event, **fields):
        record = {"event": event, "sequence": self.sequence, "time_ns": time.time_ns()}
        record.update(fields)
        backup.write_all(
            self.journal_fd,
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        os.fsync(self.journal_fd)
        self.sequence += 1

    def pending(self, packet):
        command, address = validate_write_packet(packet)
        self.packet_id += 1
        self.append(
            "pending",
            packet_id=self.packet_id,
            command=command,
            address=address,
            page=address & ~(PAGE_SIZE - 1),
            packet_hex=packet.hex(),
        )
        return self.packet_id

    def failure(self, event, error, **fields):
        # A failed log write must never cause a device command or remove intent.
        try:
            self.append(event, error=f"{type(error).__name__}: {error}", **fields)
        except (OSError, backup.BackupError, KeyboardInterrupt) as log_error:
            error.add_note(f"journal could not record {event}: {log_error}")

    def __exit__(self, exc_type, error, traceback):
        try:
            if exc_type is not None:
                self.failure("aborted", error)
        finally:
            self.stack.close()


class InstallTransport(backup.HidrawTransport):
    """Inherit exact-node open/lock and read checks; limit application writes."""

    def enter_loader(self):
        raise backup.BackupError("installer never enters the loader")

    def exit_loader(self):
        raise backup.BackupError("installer never sends opcode 5 or exits the loader")

    def _write_request(self, packet, journal):
        self._require_mode("boot")
        command, _ = validate_write_packet(packet)
        self._require_quiet()
        packet_id = journal.pending(packet)
        reply = None
        try:
            written = os.write(self.fd, packet)
            if written != 64:
                raise backup.BackupError("short destructive write; outcome unknown")
            if not self._ready(backup.REPLY_TIMEOUT):
                raise backup.BackupError("destructive reply timed out; outcome unknown")
            reply = os.read(self.fd, 65)
            expected = bytes((5, 1, 1 if command == "erase" else 2)) + bytes(61)
            if reply != expected:
                raise backup.BackupError(
                    f"{command} failed or malformed ACK ({len(reply)} bytes): "
                    f"{reply.hex(' ')}"
                )
            self._require_quiet()
        except BaseException as error:
            journal.failure(
                "result",
                error,
                packet_id=packet_id,
                outcome=(
                    "failed_ack" if reply == bytes((5, 1, 0)) + bytes(61) else "unknown"
                ),
                reply_hex=None if reply is None else reply.hex(),
            )
            raise
        # An ACK is not proof of a whole page's contents. Readback is mandatory.
        journal.append(
            "result", packet_id=packet_id, outcome="ack", reply_hex=reply.hex()
        )

    def erase_page(self, page, journal):
        self._write_request(encode_erase(page), journal)

    def program(self, address, data, journal):
        self._write_request(encode_program(address, data), journal)


def load_candidate(path, original):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != backup.FLASH_SIZE:
        raise backup.BackupError(
            "candidate must be a 16384-byte regular non-symlink file"
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        candidate = backup.read_regular(fd, backup.FLASH_SIZE)
    finally:
        os.close(fd)
    parameters = patch.profile_parameters(candidate)
    if parameters is None:
        raise backup.BackupError(
            "candidate SHA-256 is not a pinned sleep/profile image"
        )
    expected, _ = patch.prepare(original, "pinned original backup", *parameters)
    if candidate != expected:
        raise backup.BackupError(
            "candidate differs from the generated sleep/profile patch of the original"
        )
    return candidate


def capture(transport, transaction, name, target=None):
    """Persist two passes; before/ is never a backup usable for loader exit."""
    directory = transaction.directory / name
    with backup.BackupFiles(directory, transport.node) as files:
        files.manifest["installer_snapshot"] = {
            "role": name,
            "operation": transaction.header["operation"],
            "target_sha256": transaction.header["target_sha256"],
            "journal": "../journal.jsonl",
        }
        # Keep a refusing manifest even if later file persistence fails. pwrite
        # leaves the offset at zero for BackupFiles' final manifest write.
        backup.write_all(
            files.manifest_fd, b'{"format_version":1,"status":"incomplete"}\n', 0
        )
        os.fsync(files.manifest_fd)
        os.fsync(files.dir_fd)
        os.fsync(transaction.dir_fd)
        for index in range(2):
            record = files.manifest["passes"][index]
            record["status"] = "in_progress"
            backup.read_image(
                transport,
                reverse=bool(index),
                save_chunk=partial(files.save_chunk, index),
            )
            os.fsync(files.fds[index])
            record["status"] = "complete"
        images = [backup.read_regular(fd, backup.FLASH_SIZE) for fd in files.fds]
        if (
            any(len(image) != backup.FLASH_SIZE for image in images)
            or images[0] != images[1]
        ):
            raise backup.BackupError(
                f"{name}: full-image reads differ; keep both dumps"
            )
        if target is not None and images[0] != target:
            raise backup.BackupError("after: full image differs from the exact target")
        files.manifest["images_match"] = True
        files.manifest["status"] = "snapshot_only" if target is None else "complete"
    os.fsync(transaction.dir_fd)
    transaction.append(
        "snapshot_verified",
        directory=name,
        sha256=backup.sha256(images[0]),
        status="snapshot_only" if target is None else "complete",
    )
    return images[0]


def require_current(operation, current, original):
    if len(current) != backup.FLASH_SIZE:
        raise backup.BackupError("current image is not 16384 bytes")
    if operation == "install":
        if current != original:
            parameters = patch.profile_parameters(current)
            if (
                parameters is None
                or current
                != patch.prepare(original, "pinned original backup", *parameters)[0]
            ):
                raise backup.BackupError(
                    "install requires current flash to equal the pinned original "
                    "or a generated sleep/profile image"
                )
    elif operation == "rollback":
        outside = bytearray(current)
        for page in ALLOWED_PAGES:
            outside[page : page + PAGE_SIZE] = original[page : page + PAGE_SIZE]
        if outside != original:
            raise backup.BackupError(
                "rollback refuses changed bytes outside the three allowed pages"
            )
    else:
        raise backup.BackupError("unknown operation")


def read_page(transport, page):
    if page not in ALLOWED_PAGES:
        raise backup.BackupError("page read requires an allowed application page")
    return b"".join(
        transport.read_chunk(page + offset, min(backup.CHUNK_SIZE, PAGE_SIZE - offset))
        for offset in range(0, PAGE_SIZE, backup.CHUNK_SIZE)
    )


def page_order(operation, target):
    parameters = patch.profile_parameters(target)
    if operation == "install" and parameters is not None:
        return PAGE_ORDER["install"]
    if operation == "rollback":
        fingerprint = backup.sha256(target)
        if fingerprint == ORIGINAL_SHA256:
            return PAGE_ORDER["rollback"]
        if parameters is not None and parameters[1] is None:
            return PAGE_ORDER["install"]
    raise backup.BackupError("unsupported operation or target image")


def write_pages(transport, transaction, operation, current, target):
    for page in page_order(operation, target):
        page_target = target[page : page + PAGE_SIZE]
        if current[page : page + PAGE_SIZE] == page_target:
            transaction.append("page_skipped", page=page)
            continue
        transaction.append(
            "page_start", page=page, target_sha256=backup.sha256(page_target)
        )
        transport.erase_page(page, transaction)
        if read_page(transport, page) != b"\xff" * PAGE_SIZE:
            raise backup.BackupError(f"page 0x{page:04x} is not fully erased")
        transaction.append("page_erased_verified", page=page)
        for offset in range(0, PAGE_SIZE, 58):
            transport.program(
                page + offset, page_target[offset : offset + 58], transaction
            )
        if read_page(transport, page) != page_target:
            raise backup.BackupError(f"page 0x{page:04x} does not match its target")
        transaction.append(
            "page_verified", page=page, sha256=backup.sha256(page_target)
        )


def command_update(args, *, guard=None, expected_target=None, guided=False):
    """Optionally bind the loaded target to bytes selected by a calling workflow."""
    if not args.confirm_application_writes:
        raise backup.BackupError("--confirm-application-writes is required")
    directory = backup.new_output_argument(args.out)
    node = backup.inspect_node(args.device)
    node.require("boot", args.expect_port)
    if guard is not None:
        guard(node, "boot")
    original = backup.load_backup(args.backup, node)
    if backup.sha256(original) != ORIGINAL_SHA256:
        raise backup.BackupError("backup is not the pinned V0114 original")
    if args.command == "install":
        target = load_candidate(args.candidate, original)
        interval, cpi = patch.profile_parameters(target)
        target_name = f"sleep-{interval}ms" + (f"-cpi{cpi}" if cpi is not None else "")
    elif args.command == "rollback":
        interval, cpi = args.poll_interval_ms, None
        if type(interval) is not int or interval not in patch.POLL_INTERVALS:
            raise backup.BackupError("rollback poll interval must be 10, 4, 2, or 1 ms")
        target_name = args.target
        if target_name == "original":
            if interval != 10:
                raise backup.BackupError(
                    "--target original requires the default --poll-interval-ms 10"
                )
            target = original
        elif target_name == "sleep":
            target, _ = patch.prepare(original, "pinned original backup", interval, cpi)
        else:
            raise backup.BackupError("rollback target must be original or sleep")
    else:
        raise backup.BackupError("unknown operation")
    if expected_target is not None and target != expected_target:
        raise backup.BackupError(
            "candidate no longer matches the requested target bytes/SHA-256"
        )
    order = page_order(args.command, target)
    header = {
        "operation": args.command,
        "device": node.device,
        "port": node.port,
        "identity": node.identity(),
        "paths": {
            "class": node.class_path,
            "hid": node.hid_path,
            "interface": node.interface_path,
            "usb": node.usb_path,
            "inodes": node.path_inodes,
            "device_number": node.device_number,
        },
        "source_backup": str(args.backup),
        "source_sha256": backup.sha256(original),
        "target_sha256": backup.sha256(target),
        "target": target_name,
        "target_bInterval_ms": target[patch.POLL_INTERVAL_OFFSET],
        "target_cpi_requested": cpi,
        "candidate": str(args.candidate) if args.command == "install" else None,
        "allowed_pages": list(ALLOWED_PAGES),
        "page_order": list(order),
        "ack_is_not_readback": True,
    }
    with (
        Transaction(directory, header) as transaction,
        InstallTransport.open(
            args.device, args.expect_port, "boot", guard=guard
        ) as transport,
    ):
        if transport.node != node:
            raise backup.BackupError("device identity changed after backup validation")
        print("Reading current firmware...", flush=True)
        current = capture(transport, transaction, "before")
        require_current(args.command, current, original)
        transaction.append(
            "current_accepted",
            sha256=backup.sha256(current),
            effective_page_order=[
                page
                for page in order
                if current[page : page + PAGE_SIZE] != target[page : page + PAGE_SIZE]
            ],
        )
        print("Writing changed pages...", flush=True)
        write_pages(transport, transaction, args.command, current, target)
        print("Verifying firmware...", flush=True)
        capture(transport, transaction, "after", target=target)
        transaction.append("complete", target_sha256=backup.sha256(target))
    print("Firmware verified: both full readbacks match.", flush=True)
    if not guided:
        command = shlex.join(
            [
                "python3",
                "tools/k64325_backup.py",
                "exit",
                "--device",
                str(args.device),
                "--expect-port",
                args.expect_port,
                "--backup",
                str(directory / "after"),
                "--confirm-marker-writes",
            ]
        )
        print(f"To start the firmware, run with the same permissions:\n  {command}")
    return 0


def parser():
    result = argparse.ArgumentParser(
        description=(
            "Install or restore a V0114 profile while the device is in loader mode. "
            "Writes pages 0x1f00, 0x2200 and 0x2e00. "
            "Use tools/flash-firmware for guided entry, installation, and exit."
        ),
        epilog=(
            "Close other firmware tools. The device stays in loader mode after installation. "
            + WARNING
        ),
    )
    commands = result.add_subparsers(dest="command")
    for operation, description in (
        (
            "install",
            "require original backup and known current image; write cave, hook, descriptor",
        ),
        (
            "rollback",
            "restore original or stock-CPI sleep/poll baseline; permit partial allowed pages",
        ),
    ):
        command = commands.add_parser(
            operation, help=description, description=description, epilog=WARNING
        )
        command.add_argument("--device", required=True, type=backup.device_argument)
        command.add_argument("--expect-port", required=True, type=backup.port_argument)
        command.add_argument(
            "--backup",
            required=True,
            type=Path,
            metavar="ORIGINAL_DOUBLE_BACKUP_DIRECTORY",
        )
        command.add_argument(
            "--out", required=True, type=Path, metavar="NEW_TRANSACTION_DIRECTORY"
        )
        command.add_argument(
            "--confirm-application-writes",
            action="store_true",
            required=True,
            help="confirm erasing and writing application pages",
        )
        if operation == "install":
            command.add_argument(
                "--candidate",
                required=True,
                type=Path,
                metavar="FILE",
                help="built V0114 profile binary",
            )
        else:
            command.add_argument(
                "--target",
                choices=("original", "sleep"),
                default="original",
                help="original (default) or stock-CPI sleep image at the selected poll interval",
            )
            command.add_argument(
                "--poll-interval-ms",
                type=int,
                choices=patch.POLL_INTERVALS,
                default=10,
                help="bInterval for --target sleep (default: 10); original requires 10",
            )
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 0
    try:
        return command_update(args)
    except (OSError, backup.BackupError, patch.PatchError, KeyboardInterrupt) as error:
        print(f"error: {error}\n{WARNING}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
