#!/usr/bin/env python3
"""Guarded Linux hidraw backup of the analyzed 16 KiB Expert Mouse firmware."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import k64325_patch_sleep as patch

# The loader fingerprint stops before the metadata page changed by entry/exit.
FLASH_SIZE = 0x4000
CHUNK_SIZE = 60
LOADER_END = 0x0B00
BOOT_MARKER = 0x0BEB
LOADER_SHA256 = "5522a686f9c1d1b75a962d36896a022ddda431f565f2cf8ed07f6616d377b6a5"
SYS_HIDRAW = Path("/sys/class/hidraw")
REPLY_TIMEOUT = 2.0
TRANSITION_TIMEOUT = 15.0
POLL_INTERVAL = 0.1

# Device + configuration and HID report descriptors from V0116, respectively
# [1eff:1f33], [1f33:1f67], [063f:067a], and [067a:06a0].
APP_USB = bytes.fromhex(
    "12 01 00 02 00 00 00 08 7d 04 20 10 16 01 00 01 00 01 "
    "09 02 22 00 01 01 00 a0 32 09 04 00 00 01 03 01 02 00 "
    "09 21 11 01 00 01 22 34 00 07 05 81 03 04 00 0a"
)
APP_USB_VARIANTS = {
    0x0114: tuple(
        APP_USB[:12] + b"\x14\x01" + APP_USB[14:-1] + bytes((interval,))
        for interval in patch.POLL_INTERVALS
    ),
    0x0116: (APP_USB,),
}
APP_HID = bytes.fromhex(
    "05 01 09 02 a1 01 09 01 a1 00 05 09 19 01 29 05 15 00 "
    "25 01 95 05 75 01 81 02 95 01 75 03 81 01 05 01 09 30 "
    "09 31 09 38 15 81 25 7f 75 08 95 03 81 06 c0 c0"
)
BOOT_USB = bytes.fromhex(
    "12 01 10 01 00 00 00 08 4f 1c 51 88 00 01 00 00 00 01 "
    "09 02 29 00 01 01 00 80 32 09 04 00 00 02 03 00 00 00 "
    "09 21 10 01 00 01 22 26 00 07 05 01 03 40 00 02 "
    "07 05 82 03 40 00 02"
)
BOOT_HID = bytes.fromhex(
    "06 00 ff 09 01 a1 01 85 01 95 3f 75 08 26 ff 00 15 00 "
    "09 01 91 02 85 05 95 3f 75 08 26 ff 00 15 00 09 01 81 02 c0"
)
APP_ID = (0x047D, 0x1020)
BOOT_ID = (0x1C4F, 0x8851)
UNSUPPORTED_ID = (0x404F, 0x1C51)
IMAGE_NAMES = ("dump-1.bin", "dump-2.bin")
METADATA_CAVEAT = (
    "Application-to-loader entry erases metadata page 0x0b00-0x0bff and writes "
    "boot markers. A dump taken afterward cannot recover its previous contents. "
    "This tool cannot determine whether or how this loader was entered."
)
ENTRY_WARNING = (
    "Entering the loader rewrites boot metadata (0x0b00-0x0bff). "
    "The device stays in loader mode until an exit command succeeds."
)
EXIT_WARNING = (
    "Exit rewrites boot metadata and starts the installed firmware. "
    "If that firmware fails to start, software recovery may be unavailable."
)


class BackupError(Exception):
    """An operation was refused or did not complete."""


class NotUSB(BackupError):
    """A hidraw node does not have the required USB ancestry."""


def device_argument(value):
    if re.fullmatch(r"/dev/hidraw[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("use a canonical /dev/hidrawN path")
    return value


def port_argument(value):
    if re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*", value) is None:
        raise argparse.ArgumentTypeError("use a physical USB-port path such as 3-2.1")
    return value


def read_sysfs(path, limit=4096):
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise BackupError(f"sysfs attribute is too large: {path}")
    return data


def sysfs_hex(path):
    raw = read_sysfs(path, 16).strip()
    if re.fullmatch(rb"[0-9a-fA-F]{2,4}", raw) is None:
        raise BackupError(f"invalid hexadecimal sysfs attribute: {path}")
    return int(raw, 16)


@dataclass(frozen=True)
class USBNode:
    device: str
    class_path: str
    hid_path: str
    interface_path: str
    usb_path: str
    path_inodes: tuple
    device_number: tuple
    port: str
    vid: int
    pid: int
    release: int
    interface: int
    usb_descriptor: bytes
    hid_descriptor: bytes

    def identity(self):
        return {
            "vid": f"{self.vid:04x}",
            "pid": f"{self.pid:04x}",
            "bcdDevice": f"{self.release:04x}",
            "interface": self.interface,
            "usb_descriptors_hex": self.usb_descriptor.hex(),
            "hid_report_descriptor_hex": self.hid_descriptor.hex(),
        }

    def mismatches(self, mode):
        if mode == "application":
            expected_id, expected_hid = APP_ID, APP_HID
            expected_usb = APP_USB_VARIANTS.get(self.release, ())
            releases = APP_USB_VARIANTS
        elif mode == "boot":
            expected_id, expected_hid = BOOT_ID, BOOT_HID
            expected_usb, releases = (BOOT_USB,), (0x0100,)
        else:
            raise BackupError("unknown device mode")
        reasons = []
        if (self.vid, self.pid) != expected_id:
            reasons.append("VID:PID")
        if self.release not in releases:
            reasons.append("bcdDevice")
        if self.interface != 0:
            reasons.append("interface must be 0")
        if self.usb_descriptor not in expected_usb:
            reasons.append("USB descriptors")
        if self.hid_descriptor != expected_hid:
            reasons.append("HID report descriptor")
        return reasons

    def require(self, mode, port):
        if self.port != port:
            raise BackupError(f"port mismatch: requested {port}, found {self.port}")
        if (self.vid, self.pid) == UNSUPPORTED_ID:
            raise BackupError(
                "404f:1c51 is unsupported: opcode 3 WRITES in that protocol"
            )
        reasons = self.mismatches(mode)
        if reasons:
            raise BackupError(
                f"not the exact supported {mode} device: {', '.join(reasons)}"
            )


def inspect_node(device):
    device_argument(device)
    class_link = SYS_HIDRAW / Path(device).name
    class_path = class_link.resolve(strict=True)
    hid_path = (class_link / "device").resolve(strict=True)
    interface_path = next(
        (p for p in hid_path.parents if (p / "bInterfaceNumber").is_file()), None
    )
    if interface_path is None:
        raise NotUSB(f"{device}: no USB interface parent")
    usb_path = next(
        (p for p in interface_path.parents if (p / "idVendor").is_file()), None
    )
    if usb_path is None:
        raise NotUSB(f"{device}: no USB device parent")
    dev = read_sysfs(class_link / "dev", 32).strip()
    if re.fullmatch(rb"[0-9]+:[0-9]+", dev) is None:
        raise BackupError(f"{device}: invalid sysfs device number")
    paths = (class_path, hid_path, interface_path, usb_path)
    inodes = tuple((s.st_dev, s.st_ino) for s in (p.stat() for p in paths))
    return USBNode(
        device=device,
        class_path=str(class_path),
        hid_path=str(hid_path),
        interface_path=str(interface_path),
        usb_path=str(usb_path),
        path_inodes=inodes,
        device_number=tuple(int(n) for n in dev.split(b":")),
        port=usb_path.name,
        vid=sysfs_hex(usb_path / "idVendor"),
        pid=sysfs_hex(usb_path / "idProduct"),
        release=sysfs_hex(usb_path / "bcdDevice"),
        interface=sysfs_hex(interface_path / "bInterfaceNumber"),
        usb_descriptor=read_sysfs(usb_path / "descriptors"),
        hid_descriptor=read_sysfs(hid_path / "report_descriptor"),
    )


def enumerate_nodes():
    nodes, errors = [], []
    try:
        entries = sorted(SYS_HIDRAW.iterdir())
    except OSError as error:
        return [], [f"{SYS_HIDRAW}: {error}"]
    for path in entries:
        if re.fullmatch(r"hidraw[0-9]+", path.name) is None:
            continue
        try:
            nodes.append(inspect_node(f"/dev/{path.name}"))
        except NotUSB:
            continue
        except (OSError, BackupError) as error:
            errors.append(f"{path.name}: {error}")
    return nodes, errors


def encode_read(address, count):
    # The firmware neither bounds the count nor prevents 16-bit wrap safely.
    if type(address) is not int or type(count) is not int:
        raise BackupError("read address and count must be integers")
    if not 1 <= count <= CHUNK_SIZE or not 0 <= address < FLASH_SIZE:
        raise BackupError("read requires count 1..60 and address 0x0000..0x3fff")
    if address + count > FLASH_SIZE:
        raise BackupError("read crosses the non-wrapping 0x4000 flash boundary")
    return bytes((1, 3, count, address >> 8, address & 255)) + bytes(59)


def decode_read(reply, count):
    if type(count) is not int or not 1 <= count <= CHUNK_SIZE:
        raise BackupError("reply count must be 1..60")
    if len(reply) != 64:
        raise BackupError(f"read reply must be exactly 64 bytes, received {len(reply)}")
    if reply[:4] != bytes((5, 1, 4, count)):
        raise BackupError(f"invalid read reply header: {reply[:4].hex(' ')}")
    # There is no address echo. Tail bytes are not cleared by the loader.
    return reply[4 : 4 + count]


class HidrawTransport:
    """Exact-node transport; optional guard(node, mode) can refuse each request."""

    def __init__(self, fd, node, mode, *, guard=None):
        self.fd, self.node, self.mode = fd, node, mode
        self.guard = guard

    @classmethod
    @contextmanager
    def open(cls, device, port, mode, *, guard=None):
        before = inspect_node(device)
        before.require(mode, port)
        if guard is not None:
            guard(before, mode)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(device, flags)
        except PermissionError as error:
            raise BackupError(
                f"access denied to {device}; ask an administrator to grant hidraw "
                "access (for example with a udev rule), then rerun explicitly. "
                "This tool does not execute sudo"
            ) from error
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BackupError(
                    f"{device}: another process holds the advisory lock"
                ) from error
            opened = os.fstat(fd)
            if (
                not stat.S_ISCHR(opened.st_mode)
                or (os.major(opened.st_rdev), os.minor(opened.st_rdev))
                != before.device_number
            ):
                raise BackupError(
                    "opened FD does not match the inspected sysfs character device"
                )
            after = inspect_node(device)
            if after != before:
                raise BackupError(
                    "sysfs device identity changed while opening; no command sent"
                )
            after.require(mode, port)
            if guard is not None:
                guard(after, mode)
            yield cls(fd, after, mode, guard=guard)
        finally:
            os.close(fd)

    def _require_mode(self, mode):
        if self.mode != mode:
            raise BackupError(f"operation requires {mode} mode")
        if self.guard is not None:
            self.guard(self.node, mode)

    def _ready(self, timeout):
        readable, _, exceptional = select.select([self.fd], [], [self.fd], timeout)
        if exceptional:
            raise BackupError("hidraw exception/disconnect; no retry")
        return bool(readable)

    def _require_quiet(self):
        if self._ready(0):
            raise BackupError("pending unsolicited/extra input; aborting without retry")

    def enter_loader(self):
        self._require_mode("application")
        # Unnumbered hidraw output needs ID 0. USB removes it and issues
        # SET_REPORT(Output, ID 0) on interface 0 because interrupt OUT is absent.
        packet = bytes.fromhex("00 aa 55 00 00 00 00 00 00")
        try:
            written = os.write(self.fd, packet)
        except OSError as error:
            raise BackupError(
                "entry write outcome unknown; run inspect; do not retry"
            ) from error
        if written != len(packet):
            raise BackupError(
                "short entry write; outcome unknown; run inspect; do not retry"
            )

    def read_chunk(self, address, count):
        self._require_mode("boot")
        packet = encode_read(address, count)
        self._require_quiet()
        try:
            written = os.write(self.fd, packet)
            if written != 64:
                raise BackupError("short read-request write; aborting without retry")
            if not self._ready(REPLY_TIMEOUT):
                raise BackupError("read reply timed out; aborting without retry")
            reply = os.read(self.fd, 65)
        except OSError as error:
            raise BackupError(f"read transport failed; no retry: {error}") from error
        if not reply:
            raise BackupError("device disconnected during read; no retry")
        data = decode_read(reply, count)
        self._require_quiet()
        return data

    def exit_loader(self):
        self._require_mode("boot")
        self._require_quiet()
        try:
            written = os.write(self.fd, bytes((1, 5)) + bytes(62))
        except OSError as error:
            raise BackupError(
                "exit outcome unknown; run inspect; do not retry"
            ) from error
        if written != 64:
            raise BackupError(
                "short exit write; outcome unknown; run inspect; do not retry"
            )
        # Opcode 5 queues no ACK. Do not read or send another command here.


def chunks(reverse=False):
    addresses = range(0, FLASH_SIZE, CHUNK_SIZE)
    if reverse:
        addresses = reversed(addresses)
    for address in addresses:
        yield address, min(CHUNK_SIZE, FLASH_SIZE - address)


def read_image(transport, reverse=False, save_chunk=None):
    image = bytearray(FLASH_SIZE)
    for address, count in chunks(reverse):
        data = transport.read_chunk(address, count)
        image[address : address + count] = data
        if save_chunk is not None:
            save_chunk(address, data)
    return bytes(image)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_all(fd, data, offset=None):
    view = memoryview(data)
    while view:
        count = os.write(fd, view) if offset is None else os.pwrite(fd, view, offset)
        if count <= 0:
            raise BackupError("file write made no progress")
        view = view[count:]
        if offset is not None:
            offset += count


def read_regular(fd, limit):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise BackupError("backup member is not a bounded regular file")
    data = bytearray()
    while len(data) <= limit:
        part = os.pread(fd, min(4096, limit + 1 - len(data)), len(data))
        if not part:
            return bytes(data)
        data.extend(part)
    raise BackupError("backup member exceeds its size limit")


def new_output_argument(value):
    path = Path(value)
    if os.path.lexists(path):
        raise BackupError(f"output already exists; nothing overwritten: {path}")
    if not path.parent.is_dir():
        raise BackupError(f"output parent is not an existing directory: {path.parent}")
    return path


class BackupFiles:
    def __init__(self, directory, node):
        self.directory = directory
        self.dir_fd = None
        self.fds = []
        self.manifest_fd = None
        self.manifest = {
            "format_version": 1,
            "status": "incomplete",
            "port": node.port,
            "physical_sysfs_path": node.usb_path,
            "boot_identity": node.identity(),
            "hid_sysfs_path": node.hid_path,
            "metadata_modified_on_entry_caveat": METADATA_CAVEAT,
            "loader_fingerprint_range": "[0x0000,0x0b00)",
            "analyzed_loader_sha256": LOADER_SHA256,
            "images_match": False,
            "passes": [
                {
                    "file": name,
                    "order": "reverse" if index else "forward",
                    "status": "not_started",
                    "chunks_completed": 0,
                    "bytes_completed": 0,
                }
                for index, name in enumerate(IMAGE_NAMES)
            ],
        }

    def __enter__(self):
        os.mkdir(self.directory, 0o700)
        try:
            self.dir_fd = os.open(
                self.directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.fchmod(self.dir_fd, 0o700)
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            self.manifest_fd = os.open(
                "manifest.json", flags, 0o600, dir_fd=self.dir_fd
            )
            os.fchmod(self.manifest_fd, 0o600)
            for name in IMAGE_NAMES:
                fd = os.open(name, flags, 0o600, dir_fd=self.dir_fd)
                self.fds.append(fd)
                os.fchmod(fd, 0o600)
            os.fsync(self.dir_fd)
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def save_chunk(self, index, address, data):
        write_all(self.fds[index], data, address)
        record = self.manifest["passes"][index]
        record["chunks_completed"] += 1
        record["bytes_completed"] += len(data)

    def __exit__(self, exc_type, error, traceback):
        try:
            if exc_type is not None:
                self.manifest["status"] = "incomplete"
                self.manifest["error"] = f"{exc_type.__name__}: {error}"
            for index, fd in enumerate(self.fds):
                record = self.manifest["passes"][index]
                if record["status"] == "in_progress":
                    record["status"] = "incomplete"
                os.fsync(fd)
                data = read_regular(fd, FLASH_SIZE)
                record["size"] = len(data)
                record["sha256"] = sha256(data)
                if record["status"] == "complete":
                    record["loader_code_sha256"] = sha256(data[:LOADER_END])
            if self.manifest_fd is not None:
                write_all(
                    self.manifest_fd,
                    (
                        json.dumps(self.manifest, indent=2, sort_keys=True) + "\n"
                    ).encode(),
                )
                os.fsync(self.manifest_fd)
            if self.dir_fd is not None:
                os.fsync(self.dir_fd)
        finally:
            for fd in self.fds:
                os.close(fd)
            if self.manifest_fd is not None:
                os.close(self.manifest_fd)
            if self.dir_fd is not None:
                os.close(self.dir_fd)


def load_backup(directory, node):
    dir_fd = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:

        def member(name, limit):
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=dir_fd,
            )
            try:
                return read_regular(fd, limit)
            finally:
                os.close(fd)

        try:
            manifest = json.loads(member("manifest.json", 65536))
        except (ValueError, UnicodeError) as error:
            raise BackupError("invalid backup manifest JSON") from error
        if not isinstance(manifest, dict) or (
            manifest.get("format_version") != 1
            or manifest.get("status") != "complete"
            or manifest.get("images_match") is not True
            or manifest.get("loader_fingerprint_range") != "[0x0000,0x0b00)"
            or manifest.get("analyzed_loader_sha256") != LOADER_SHA256
        ):
            raise BackupError(
                "backup manifest does not record a complete matching double backup"
            )
        if (
            manifest.get("port") != node.port
            or manifest.get("physical_sysfs_path") != node.usb_path
            or manifest.get("boot_identity") != node.identity()
        ):
            raise BackupError(
                "backup physical port/path or boot identity does not match this device"
            )
        passes = manifest.get("passes")
        if not isinstance(passes, list) or len(passes) != 2:
            raise BackupError("backup manifest must contain two passes")
        images = []
        for index, name in enumerate(IMAGE_NAMES):
            image = member(name, FLASH_SIZE)
            record = passes[index]
            if (
                not isinstance(record, dict)
                or record.get("file") != name
                or record.get("status") != "complete"
                or record.get("order") != ("reverse" if index else "forward")
                or record.get("bytes_completed") != FLASH_SIZE
                or record.get("chunks_completed")
                != len(range(0, FLASH_SIZE, CHUNK_SIZE))
                or record.get("size") != FLASH_SIZE
                or len(image) != FLASH_SIZE
                or record.get("sha256") != sha256(image)
                or record.get("loader_code_sha256") != sha256(image[:LOADER_END])
            ):
                raise BackupError(
                    f"backup pass is incomplete, corrupt, or hash-mismatched: {name}"
                )
            images.append(image)
        if images[0] != images[1]:
            raise BackupError("raw backup files differ")
        fingerprint = sha256(images[0][:LOADER_END])
        if fingerprint != LOADER_SHA256:
            raise BackupError(
                f"unknown loader code SHA-256 {fingerprint}; exit refused. "
                "Keep the dump for analysis"
            )
        if images[0][BOOT_MARKER] != 0x55:
            raise BackupError("backup boot marker at 0x0beb is not 0x55; exit refused")
        return images[0]
    finally:
        os.close(dir_fd)


def wait_for_mode(node, mode, expected_usb=None):
    deadline = time.monotonic() + TRANSITION_TIMEOUT
    while True:
        nodes, _ = enumerate_nodes()
        matches = [
            candidate
            for candidate in nodes
            if candidate.port == node.port
            and candidate.usb_path == node.usb_path
            and not candidate.mismatches(mode)
            and (expected_usb is None or candidate.usb_descriptor == expected_usb)
        ]
        if len(matches) > 1:
            raise BackupError(
                "multiple matching nodes on the expected port; run inspect"
            )
        if matches:
            return matches[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(POLL_INTERVAL, remaining))


def require_confirmation(args):
    if not args.confirm_marker_writes:
        raise BackupError(
            "requires --confirm-marker-writes: this operation erases/writes the metadata page"
        )


def command_inspect(args):
    nodes, errors = enumerate_nodes()
    found = False
    for node in nodes:
        pair = node.vid, node.pid
        if pair not in (APP_ID, BOOT_ID, UNSUPPORTED_ID):
            continue
        found = True
        if pair == UNSUPPORTED_ID:
            result = "UNSUPPORTED (opcode 3 WRITES); descriptors not supported"
        else:
            mode = "application" if pair == APP_ID else "boot"
            reasons = node.mismatches(mode)
            result = (
                f"{mode}: exact descriptors/identity match"
                if not reasons
                else (f"{mode}: MISMATCH ({', '.join(reasons)})")
            )
            if mode == "application" and not reasons:
                result += f"; descriptor bInterval={node.usb_descriptor[-1]} ms"
        print(
            f"{node.device} port={node.port} {node.vid:04x}:{node.pid:04x} "
            f"bcdDevice={node.release:04x} interface={node.interface:02x} {result}\n"
            f"  USB sysfs: {node.usb_path}"
        )
    for error in errors:
        print(f"inspect: {error}", file=sys.stderr)
    if not found:
        print("No Expert Mouse found. Check its USB connection.")
    return 0


def command_enter(args):
    require_confirmation(args)
    with HidrawTransport.open(
        args.device, args.expect_port, "application"
    ) as transport:
        print(ENTRY_WARNING, file=sys.stderr, flush=True)
        transport.enter_loader()
        node = transport.node
    observed = wait_for_mode(node, "boot")
    if observed is None:
        raise BackupError(
            "entry outcome unknown: boot device not observed on this port; run inspect; do not retry"
        )
    print(f"Loader ready: {observed.device}, port {observed.port}.")
    return 0


def command_dump(args, *, guard=None):
    directory = new_output_argument(args.out)
    with HidrawTransport.open(
        args.device, args.expect_port, "boot", guard=guard
    ) as transport:
        try:
            with BackupFiles(directory, transport.node) as backup:
                for index in range(2):
                    record = backup.manifest["passes"][index]
                    record["status"] = "in_progress"
                    read_image(
                        transport,
                        reverse=bool(index),
                        save_chunk=partial(backup.save_chunk, index),
                    )
                    os.fsync(backup.fds[index])
                    record["status"] = "complete"
                # Both raw files have been persisted before comparing their contents.
                images = [read_regular(fd, FLASH_SIZE) for fd in backup.fds]
                hashes = [sha256(image) for image in images]
                if (
                    any(len(image) != FLASH_SIZE for image in images)
                    or images[0] != images[1]
                ):
                    raise BackupError(
                        "double read mismatch; both raw dumps kept; no exit sent"
                    )
                backup.manifest["images_match"] = True
                backup.manifest["status"] = "complete"
        except BaseException:
            print(
                f"Dump incomplete; keep partial files in {directory}.",
                file=sys.stderr,
            )
            raise
    print(f"Matching 16384-byte dumps saved in {directory}\nSHA-256: {hashes[0]}")
    return 0


def command_exit(args):
    require_confirmation(args)
    node = inspect_node(args.device)
    node.require("boot", args.expect_port)
    image = load_backup(args.backup, node)
    # Known V0114 hashes bind enumeration to a specific release and interval.
    # A known loader does not identify other applications.
    expected_usb = (
        image[patch.APP_USB_START : patch.POLL_INTERVAL_OFFSET + 1]
        if sha256(image) in (patch.SOURCE_SHA256, *patch.PROFILE_SHA256.values())
        else None
    )
    with HidrawTransport.open(args.device, args.expect_port, "boot") as transport:
        if transport.node != node:
            raise BackupError(
                "device identity changed after backup validation; exit refused"
            )
        current = read_image(transport)
        if current != image:
            raise BackupError(
                "current full flash differs from the backup; exit refused"
            )
        print(EXIT_WARNING, file=sys.stderr, flush=True)
        transport.exit_loader()
    observed = wait_for_mode(node, "application", expected_usb=expected_usb)
    if observed is None:
        raise BackupError(
            "exit outcome unknown: application not observed on this port; run inspect; do not retry"
        )
    print(
        f"Observed application {observed.device} on port {observed.port}, "
        f"bcdDevice={observed.release:04x}, "
        f"descriptor bInterval={observed.usb_descriptor[-1]} ms."
    )
    return 0


def parser():
    result = argparse.ArgumentParser(
        description=(
            "Read and back up the 16 KiB Expert Mouse firmware on Linux. "
            "Use tools/backup-firmware for guided entry, backup, and exit."
        ),
        epilog=(
            "Entry/exit rewrite boot metadata. Use this tool with the supported V0114 hardware. "
            "Close other firmware tools and keep the device connected. "
            "Reply timeout: 2 seconds; transition observation: 15 seconds."
        ),
    )
    commands = result.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="show device identity from sysfs")
    inspect.set_defaults(handler=command_inspect)
    for name, help_text, handler in (
        (
            "enter",
            "write entry markers once; observe boot enumeration only",
            command_enter,
        ),
        (
            "dump",
            "save two full raw dumps; second pass reverses chunk order",
            command_dump,
        ),
        (
            "exit",
            "verify backup and full current flash, then write exit markers once",
            command_exit,
        ),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--device", required=True, type=device_argument)
        command.add_argument("--expect-port", required=True, type=port_argument)
        if name == "dump":
            command.add_argument("--out", required=True, metavar="NEW_DIRECTORY")
        else:
            command.add_argument(
                "--confirm-marker-writes",
                action="store_true",
                help="confirm boot-metadata writes",
            )
        if name == "exit":
            command.add_argument("--backup", required=True, metavar="DIRECTORY")
        command.set_defaults(handler=handler)
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if not hasattr(args, "handler"):
        arguments.print_help()
        return 0
    try:
        if sys.platform != "linux":
            raise BackupError("this CLI requires Linux hidraw and sysfs")
        return args.handler(args)
    except (BackupError, OSError, argparse.ArgumentTypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. Keep the device connected and run inspect before another operation.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
