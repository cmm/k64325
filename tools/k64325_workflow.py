"""Interactive, single-port backup and install workflows; no I/O on import."""

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import k64325_backup as backup
import k64325_build as build
import k64325_install as install
import k64325_patch_sleep as patch

LOCK_PATH = Path("/run/lock/k64325-workflow.lock")
SYS_USB_DEVICES = Path("/sys/bus/usb/devices")
CANDIDATE_IDS = (backup.APP_ID, backup.BOOT_ID, backup.UNSUPPORTED_ID)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
INSPECT = "python3 tools/k64325_backup.py inspect"
IDENTITY_WARNING = (
    "Keep this device connected to the same USB port until the workflow finishes."
)
COMPATIBILITY_WARNING = (
    "Before entry, check the serial-number list at:\n"
    "https://www.kensington.com/internal-support-pages/k64325-expert-mouse-wired-support-page/\n"
    "Stop if your unit is eligible for V0116, even if it currently reports 0114. "
    "Entering an incompatible unit's loader can leave it stuck there."
)


def absolute_path(value):
    path = Path(value)
    if ".." in path.parts:
        raise backup.BackupError("paths must not contain '..'")
    return path.absolute()


@contextmanager
def open_directory(path, *, create=False):
    """Open every component with NOFOLLOW, including missing output ancestors."""
    path = absolute_path(path)
    with ExitStack() as stack:
        fd = os.open(path.anchor, DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            stack.callback(os.close, fd)
        yield fd


def directory_path(value, *, create=False):
    path = absolute_path(value)
    with open_directory(path, create=create):
        return path


def file_member(fd, name, limit):
    member = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
    try:
        return backup.read_regular(member, limit)
    finally:
        os.close(member)


def json_member(fd, name):
    try:
        return json.loads(file_member(fd, name, 65536))
    except (ValueError, UnicodeError) as error:
        raise backup.BackupError(f"invalid JSON in {name}") from error


def inode(info):
    return info.st_dev, info.st_ino


def fresh_directory(root, prefix):
    with open_directory(root) as root_fd:
        for _ in range(100):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            name = f"{prefix}-{stamp}-{secrets.token_hex(6)}"
            try:
                os.mkdir(name, 0o700, dir_fd=root_fd)
            except FileExistsError:
                continue
            with open_directory(root / name) as fd:
                os.fchmod(fd, 0o700)
                os.fsync(fd)
            os.fsync(root_fd)
            return root / name
    raise backup.BackupError("could not allocate a fresh output directory")


class WorkflowLock:
    """Shared across both workflows and mode changes; unrelated software may ignore it."""

    def __init__(self, path=None):
        self.path = LOCK_PATH if path is None else path
        self.stack = ExitStack()

    def __enter__(self):
        try:
            parent_fd = self.stack.enter_context(open_directory(self.path.parent))
            self.fd = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | FILE_FLAGS,
                0o600,
                dir_fd=parent_fd,
            )
            self.stack.callback(os.close, self.fd)
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise backup.BackupError(
                    f"unsafe workflow lock: {self.path}; requires an owned regular "
                    "file with mode 0600 and one link"
                )
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise backup.BackupError(
                    "another firmware workflow holds the shared process lock"
                ) from error
            self.snapshot = inode(info)
            self.check()
            os.fsync(self.fd)
            os.fsync(parent_fd)
        except PermissionError as error:
            self.stack.close()
            raise backup.BackupError(
                f"cannot access {self.path}; ask an administrator to provision "
                "this shared lock for your user (0600) and grant hidraw access"
            ) from error
        except BaseException:
            self.stack.close()
            raise
        return self

    def check(self):
        with open_directory(self.path.parent) as fd:
            info = os.stat(self.path.name, dir_fd=fd, follow_symlinks=False)
        if (
            inode(info) != self.snapshot
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise backup.BackupError("workflow lock file was replaced or made unsafe")

    def __exit__(self, *exc):
        self.stack.close()


class RunFiles:
    def __init__(self, root, action):
        self.directory = fresh_directory(root, action)
        self.output = self.directory / ("backup" if action == "backup" else "install")
        self.stack = ExitStack()

    def __enter__(self):
        print(f"Run directory: {self.directory}", flush=True)
        try:
            self.fd = self.stack.enter_context(open_directory(self.directory))
            self.snapshot = inode(os.fstat(self.fd))
            self.journal = os.open(
                "workflow.jsonl",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                0o600,
                dir_fd=self.fd,
            )
            self.stack.callback(os.close, self.journal)
            os.fchmod(self.journal, 0o600)
            os.fsync(self.fd)
            self.log("started", output=str(self.output))
            self.preflight_output()
        except BaseException:
            self.stack.close()
            raise
        return self

    def check(self):
        with open_directory(self.directory) as fd:
            if inode(os.fstat(fd)) != self.snapshot:
                raise backup.BackupError("run directory was replaced")

    def preflight_output(self):
        self.check()
        backup.new_output_argument(self.output)

    def log(self, event, **fields):
        record = {"event": event, "time_ns": time.time_ns(), **fields}
        backup.write_all(
            self.journal, (json.dumps(record, sort_keys=True) + "\n").encode()
        )
        os.fsync(self.journal)

    @contextmanager
    def operation(self, action, **fields):
        self.log("intent", action=action, **fields)
        try:
            yield
        except BaseException as error:
            self.failure("result", error, action=action, outcome="failed_or_unknown")
            raise
        self.log("result", action=action, outcome="complete")

    def failure(self, event, error, **fields):
        try:
            self.log(event, error=f"{type(error).__name__}: {error}", **fields)
        except (OSError, backup.BackupError, KeyboardInterrupt) as log_error:
            error.add_note(f"workflow journal could not record failure: {log_error}")

    def __exit__(self, exc_type, error, traceback):
        try:
            if exc_type is not None:
                self.failure("stopped", error)
        finally:
            self.stack.close()


@dataclass(frozen=True)
class USBDevice:
    usb_path: str
    port: str
    path_inode: tuple
    vid: int
    pid: int
    release: int
    usb_descriptor: bytes

    def matches(self, node):
        return (
            self.usb_path == node.usb_path
            and self.port == node.port
            and self.path_inode == node.path_inodes[-1]
            and (self.vid, self.pid, self.release, self.usb_descriptor)
            == (node.vid, node.pid, node.release, node.usb_descriptor)
        )


def sysfs_member(directory_fd, name, limit):
    # sysfs attributes report nominal file sizes, not their readable byte count.
    fd = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise backup.BackupError(f"sysfs attribute is not a regular file: {name}")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise backup.BackupError(f"sysfs attribute is too large: {name}")
    return data


def usb_hex(directory_fd, name):
    raw = sysfs_member(directory_fd, name, 16)
    if re.fullmatch(rb"[0-9a-fA-F]{4}\n?", raw) is None:
        raise backup.BackupError(f"invalid hexadecimal USB sysfs attribute: {name}")
    return int(raw, 16)


def inspect_usb_device(entry):
    path = entry.resolve(strict=True)
    if path.name != entry.name:
        raise backup.BackupError("USB bus entry and physical port name disagree")
    with open_directory(path) as fd:
        snapshot = inode(os.fstat(fd))
        pair = usb_hex(fd, "idVendor"), usb_hex(fd, "idProduct")
        device = (
            USBDevice(
                str(path),
                entry.name,
                snapshot,
                *pair,
                usb_hex(fd, "bcdDevice"),
                sysfs_member(fd, "descriptors", 4096),
            )
            if pair in CANDIDATE_IDS
            else None
        )
        if pair != (usb_hex(fd, "idVendor"), usb_hex(fd, "idProduct")):
            raise backup.BackupError("USB identity changed during inspection")
        if entry.resolve(strict=True) != path or inode(path.stat()) != snapshot:
            raise backup.BackupError("physical USB directory changed during inspection")
    return device


def enumerate_usb_devices():
    devices, errors = [], []
    try:
        # Interface entries lack device IDs. Do not use is_file()/glob() to hide
        # missing attributes or directory-listing errors on physical devices.
        entries = sorted(SYS_USB_DEVICES.iterdir())
    except OSError as error:
        return [], [f"{SYS_USB_DEVICES}: {error}"]
    for entry in entries:
        if (
            re.fullmatch(r"(?:usb[0-9]+|[0-9]+-[0-9]+(?:\.[0-9]+)*)", entry.name)
            is None
        ):
            continue
        try:
            device = inspect_usb_device(entry)
            if device is not None:
                devices.append(device)
        except (OSError, backup.BackupError) as error:
            errors.append(f"{entry.name}: {error}")
    return devices, errors


def usb_mode(device):
    pair = device.vid, device.pid
    if pair == backup.UNSUPPORTED_ID:
        raise backup.BackupError(
            "404f:1c51 is unsupported: opcode 3 WRITES in that protocol"
        )
    if pair == backup.APP_ID:
        mode, release, descriptors = (
            "application",
            0x0114,
            backup.APP_USB_VARIANTS[0x0114],
        )
    elif pair == backup.BOOT_ID:
        mode, release, descriptors = "boot", 0x0100, (backup.BOOT_USB,)
    else:
        raise backup.BackupError("unexpected USB device ID")
    if device.release != release:
        raise backup.BackupError(
            f"guided workflows require {mode} release {release:04x}"
        )
    if device.usb_descriptor not in descriptors:
        raise backup.BackupError(f"not the exact supported {mode} USB descriptors")
    if not Path(device.usb_path).is_absolute():
        raise backup.BackupError("physical USB path must be absolute")
    return mode


def discover(*, allow_pending=False):
    devices, usb_errors = enumerate_usb_devices()
    nodes, errors = backup.enumerate_nodes()
    if usb_errors or errors:
        raise backup.BackupError(
            "USB enumeration failed: " + "; ".join((*usb_errors, *errors))
        )
    if len(devices) > 1:
        raise backup.BackupError(
            f"requires exactly one physical USB candidate globally; found {len(devices)}, "
            "including devices without hidraw bindings"
        )
    device = devices[0] if devices else None
    found = [
        node
        for node in nodes
        if (node.vid, node.pid) in CANDIDATE_IDS
        or (device is not None and node.usb_path == device.usb_path)
    ]
    if len(found) > 1:
        raise backup.BackupError(
            f"requires exactly one associated hidraw node globally; found {len(found)}"
        )
    if device is None:
        if found:
            raise backup.BackupError(
                "USB/HID identity/path disagreement: hidraw has no USB bus candidate"
            )
        if allow_pending:
            return None, None
        raise backup.BackupError(
            "requires exactly one physical USB candidate globally; found 0"
        )
    usb_mode(device)
    if not found:
        if allow_pending:
            return device, None
        raise backup.BackupError(
            "matching USB device has no usable associated hidraw node"
        )
    node = found[0]
    if not device.matches(node):
        raise backup.BackupError("USB/HID identity/path disagreement")
    node_mode(node)
    return device, node


def candidates(*, allow_absent=False):
    _, node = discover(allow_pending=allow_absent)
    return node


def node_mode(node):
    mode = usb_mode(node)
    node.require(mode, node.port)
    return mode


def canonical_boot(node):
    # Application enumeration is not evidence of the installed loader's identity.
    return replace(
        node,
        vid=backup.BOOT_ID[0],
        pid=backup.BOOT_ID[1],
        release=0x0100,
        interface=0,
        usb_descriptor=backup.BOOT_USB,
        hid_descriptor=backup.BOOT_HID,
    )


class Binding:
    def __init__(self, node, lock, files):
        self.mode = node_mode(node)
        self.node, self.lock, self.files = node, lock, files
        self.port, self.usb_path = node.port, node.usb_path
        self.loader_state = "loader" if self.mode == "boot" else "application"
        self.describe("Selected")
        print(IDENTITY_WARNING, flush=True)
        files.log(
            "selected",
            mode=self.mode,
            identity=node.identity(),
            port=self.port,
            physical_sysfs_path=self.usb_path,
            device=node.device,
        )

    def describe(self, stage):
        node = self.node
        print(
            f"{stage}: {node.device}, port {self.port}, "
            f"{self.mode}, firmware {node.release:04x}",
            flush=True,
        )

    def guard(self, node, mode):
        self.check()
        if node != self.node or mode != self.mode:
            raise backup.BackupError(
                "transport does not match the bound phase snapshot"
            )

    def check(self):
        self.lock.check()
        self.files.check()
        current = candidates()
        node_mode(current)
        if current != self.node:
            raise backup.BackupError("device changed or moved during the operation")

    def confirm(self, action, warning):
        self.check()
        print(warning, file=sys.stderr, flush=True)
        require_tty()
        phrase = f"{action} {self.port}"
        try:
            response = input(f"Type '{phrase}' to continue, anything else to stop: ")
        except EOFError as error:
            raise backup.BackupError("confirmation ended at EOF; stopped") from error
        self.check()
        if response.strip() != phrase:
            raise backup.BackupError(f"{action} declined; stopped")

    def transition(self, mode, *, expected_usb=None):
        old = self.node
        deadline = time.monotonic() + backup.TRANSITION_TIMEOUT
        while True:
            self.lock.check()
            self.files.check()
            device, current = discover(allow_pending=True)
            if device is not None:
                observed_mode = usb_mode(device)
                if device.port != self.port or device.usb_path != self.usb_path:
                    raise backup.BackupError(
                        "candidate moved from the bound physical port/path"
                    )
                if observed_mode == mode:
                    if (
                        expected_usb is not None
                        and device.usb_descriptor != expected_usb
                    ):
                        raise backup.BackupError(
                            "application USB descriptors differ from the verified image"
                        )
                    if current is not None:
                        self.node, self.mode = current, mode
                        self.loader_state = (
                            "loader" if mode == "boot" else "application"
                        )
                        self.describe("Observed after transition")
                        self.files.log(
                            "transition_verified",
                            mode=mode,
                            identity=current.identity(),
                            device=current.device,
                            port=self.port,
                            physical_sysfs_path=self.usb_path,
                        )
                        return
                elif not device.matches(old) or (
                    current is not None and current != old
                ):
                    raise backup.BackupError(
                        "unexpected old-mode node replacement during transition"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise backup.BackupError(
                    f"{mode} transition timed out; outcome unknown. Do not repeat the command"
                )
            time.sleep(min(backup.POLL_INTERVAL, remaining))

    def enter(self):
        if self.mode == "boot":
            self.check()
            self.describe("Already in loader; entry skipped")
            return
        self.confirm(
            "enter",
            f"{COMPATIBILITY_WARNING}\n{backup.ENTRY_WARNING}",
        )
        self.files.preflight_output()
        with self.files.operation("entry", port=self.port):
            with backup.HidrawTransport.open(
                self.node.device,
                self.port,
                "application",
                guard=self.guard,
            ) as transport:
                self.loader_state = "entry outcome unknown"
                transport.enter_loader()
            self.transition("boot")

    def exit(self, directory, *, expected_target=None):
        self.check()
        image = backup.load_backup(directory, self.node)
        if expected_target is not None and image != expected_target:
            raise backup.BackupError(
                "saved exit backup differs from the requested target"
            )
        if backup.sha256(image) not in (
            patch.SOURCE_SHA256,
            *patch.PROFILE_SHA256.values(),
        ):
            raise backup.BackupError(
                "unknown application image saved; guided exit refused. Keep the backup for analysis"
            )
        self.confirm("exit", backup.EXIT_WARNING)
        with self.files.operation("exit", port=self.port, sha256=backup.sha256(image)):
            with backup.HidrawTransport.open(
                self.node.device,
                self.port,
                "boot",
                guard=self.guard,
            ) as transport:
                if backup.read_image(transport) != image:
                    raise backup.BackupError(
                        "live full image changed since saved readback; exit refused"
                    )
                self.loader_state = "exit outcome unknown"
                transport.exit_loader()
            expected_usb = image[patch.APP_USB_START : patch.POLL_INTERVAL_OFFSET + 1]
            self.transition("application", expected_usb=expected_usb)

    def stopped(self):
        if self.loader_state != "application":
            print(
                f"Stopped; last state: {self.loader_state}.\n"
                "Keep the device connected. Inspect it before retrying or exiting:\n"
                f"  {INSPECT}\n"
                f"Logs and readbacks: {self.files.directory}",
                file=sys.stderr,
                flush=True,
            )


def require_tty():
    if not sys.stdin.isatty():
        raise backup.BackupError("run this command in an interactive terminal")


def disjoint(first, second):
    if first == second or first in second.parents or second in first.parents:
        raise backup.BackupError(f"input/output paths overlap: {first} and {second}")


def verify_cache(directory, candidate, manifest):
    with open_directory(directory) as fd:
        image = file_member(fd, manifest["output"]["file"], backup.FLASH_SIZE)
        cached = json_member(fd, "manifest.json")
    if image != candidate:
        raise backup.BackupError(
            "cached binary differs from the exact requested profile"
        )
    if (
        not isinstance(cached, dict)
        or not isinstance(cached.get("source"), dict)
        or not isinstance(cached["source"].get("path"), str)
    ):
        raise backup.BackupError("cached manifest has an invalid source declaration")
    # A copied original backup has the same bytes; its path is provenance, not identity.
    cached["source"]["path"] = manifest["source"]["path"]
    if json.dumps(cached, sort_keys=True) != json.dumps(manifest, sort_keys=True):
        raise backup.BackupError(
            "cached manifest differs from the requested profile/hash/changes"
        )


def cached_candidate(root, source, original, cpi, rate, force, files):
    interval = build.POLL_INTERVALS_MS[rate]
    candidate, manifest = patch.prepare(
        original,
        source / backup.IMAGE_NAMES[0],
        interval,
        None if cpi == "stock" else int(cpi),
    )
    stem = build.profile_stem(cpi, rate)
    manifest["output"]["file"] = f"{stem}.bin"
    directory = root / stem
    disjoint(source, directory)
    disjoint(files.directory, directory)
    with open_directory(root) as root_fd:
        try:
            info = os.stat(stem, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                raise backup.BackupError(
                    "cache must be a non-symlink directory, even with --force-build"
                )
            if not force:
                try:
                    verify_cache(directory, candidate, manifest)
                except (OSError, backup.BackupError) as error:
                    raise backup.BackupError(
                        f"cache refused: {error}; --force-build archives and rebuilds artifacts only"
                    ) from error
                print("Using cached firmware.", flush=True)
                files.log(
                    "cache_reused",
                    directory=str(directory),
                    sha256=backup.sha256(candidate),
                )
                return directory / manifest["output"]["file"], candidate
            archive = fresh_directory(root, "archive")
            with open_directory(archive) as archive_fd:
                if os.stat(stem, dir_fd=root_fd, follow_symlinks=False) != info:
                    raise backup.BackupError("cache changed before archive; stopped")
                files.log(
                    "cache_archive_intent",
                    source=str(directory),
                    archive=str(archive / stem),
                )
                os.rename(stem, stem, src_dir_fd=root_fd, dst_dir_fd=archive_fd)
                os.fsync(archive_fd)
                os.fsync(root_fd)
            print(f"Previous cache preserved: {archive / stem}", flush=True)
            files.log("cache_archived", directory=str(archive / stem))
    patch.write_output(directory, candidate, manifest)
    verify_cache(directory, candidate, manifest)
    print("Built firmware.", flush=True)
    files.log("cache_built", directory=str(directory), sha256=backup.sha256(candidate))
    return directory / manifest["output"]["file"], candidate


def run_backup(binding, files):
    binding.enter()
    binding.describe("Double backup")
    files.preflight_output()
    with files.operation("dump", port=binding.port):
        backup.command_dump(
            SimpleNamespace(
                device=binding.node.device,
                expect_port=binding.port,
                out=files.output,
            ),
            guard=binding.guard,
        )
        image = backup.load_backup(files.output, binding.node)
    if backup.sha256(image) == patch.SOURCE_SHA256:
        print(
            "Original firmware backup verified; use it with --backup when flashing.",
            flush=True,
        )
    else:
        print(
            "Modified firmware snapshot saved. Use your original backup when flashing.",
            flush=True,
        )
    binding.exit(files.output)
    print(f"Backup: {files.output}", flush=True)


def run_flash(args, source, root, binding, files):
    original = backup.load_backup(source, canonical_boot(binding.node))
    if backup.sha256(original) != patch.SOURCE_SHA256:
        raise backup.BackupError(
            "flash-firmware requires the exact pinned original double backup"
        )
    candidate_path, target = cached_candidate(
        root,
        source,
        original,
        args.cpi,
        args.poll_rate_hz,
        args.force_build,
        files,
    )
    files.preflight_output()
    print(f"Firmware: {candidate_path}", flush=True)
    binding.enter()
    # Revalidate against the actual observed loader, not the canonical expectation.
    if backup.load_backup(source, binding.node) != original:
        raise backup.BackupError("original backup changed since preflight")
    interval = build.POLL_INTERVALS_MS[args.poll_rate_hz]
    binding.confirm(
        "install",
        f"Install CPI setting {args.cpi}, USB interval {interval} ms ({args.poll_rate_hz} Hz requested).",
    )
    files.preflight_output()
    with files.operation("install", port=binding.port, sha256=backup.sha256(target)):
        install.command_update(
            SimpleNamespace(
                command="install",
                confirm_application_writes=True,
                device=binding.node.device,
                expect_port=binding.port,
                backup=source,
                candidate=candidate_path,
                out=files.output,
            ),
            guard=binding.guard,
            expected_target=target,
            guided=True,
        )
    binding.exit(files.output / "after", expected_target=target)
    print(
        f"Installed CPI setting {args.cpi}, USB interval {interval} ms. Device is back in application mode.",
        flush=True,
    )
    print(f"Readbacks and log: {files.output}", flush=True)


def parser(action):
    flashing = action == "flash"
    cli = argparse.ArgumentParser(
        prog=f"{action}-firmware",
        description=(
            "Build or reuse a V0114 profile, flash it, verify it, and restart the device."
            if flashing
            else "Save two matching V0114 firmware reads and restart the device."
        )
        + " Connect one Expert Mouse. Starts in application or loader mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            + (
                "  sudo tools/flash-firmware --backup backup-v0114 --cpi 1000 --poll-rate-hz 250\n"
                "Uses your existing original backup and reuses a matching build.\n"
                "Readbacks and logs: work/flash-TIMESTAMP-RANDOM/\n"
                if flashing
                else "  sudo tools/backup-firmware\n"
                "Backup: work/backup-TIMESTAMP-RANDOM/backup/\n"
                "Keep the original firmware backup for future flashing.\n"
            )
            + "\nBefore starting:\n"
            + COMPATIBILITY_WARNING
            + "\n"
            + backup.ENTRY_WARNING
            + "\n"
            + backup.EXIT_WARNING
            + "\n"
            + (
                install.WARNING
                + "\n"
                + build.RATE_WARNING
                + "\n"
                + "Measure effective CPI and delivery rate with tools/measure-motion."
                + "\n"
                if flashing
                else ""
            )
            + "Type the displayed action and USB port at each confirmation.\n"
            "Close other firmware tools. Run with device and /run/lock access (e.g. sudo).\n"
            + IDENTITY_WARNING
        ),
    )
    cli.add_argument(
        "--out-root",
        type=Path,
        default=Path("work"),
        help="output/cache root (default: work); missing directories created privately",
    )
    if flashing:
        cli.add_argument(
            "--backup",
            required=True,
            type=Path,
            metavar="ORIGINAL_DOUBLE_BACKUP_DIR",
            help="existing original backup directory for this device and USB port",
        )
        cli.add_argument(
            "--cpi",
            required=True,
            choices=("stock", *(str(cpi) for cpi in patch.CPI_CHOICES)),
            help="sensor CPI setting, or stock configuration",
        )
        cli.add_argument(
            "--poll-rate-hz",
            required=True,
            type=int,
            choices=tuple(build.POLL_INTERVALS_MS),
            help="USB interval: 100/250/500/1000 Hz selects 10/4/2/1 ms; shorter than 10 ms is experimental",
        )
        cli.add_argument(
            "--force-build",
            action="store_true",
            help="archive the cached build and generate it again",
        )
    return cli


def main(action, argv=None):
    args = parser(action).parse_args(argv)
    binding = None
    try:
        if sys.platform != "linux":
            raise backup.BackupError("these workflows require Linux hidraw and sysfs")
        require_tty()
        source = directory_path(args.backup) if action == "flash" else None
        root = absolute_path(args.out_root)
        if source is not None:
            if source == root or source in root.parents:
                raise backup.BackupError(
                    "output root must not be inside the original backup"
                )
            disjoint(source, root / build.profile_stem(args.cpi, args.poll_rate_hz))
        root = directory_path(root, create=True)
        with RunFiles(root, action) as files, WorkflowLock() as lock:
            binding = Binding(candidates(), lock, files)
            try:
                if action == "backup":
                    run_backup(binding, files)
                else:
                    run_flash(args, source, root, binding, files)
                files.log("complete")
            except BaseException:
                binding.stopped()
                raise
        return 0
    except (OSError, backup.BackupError, patch.PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted.",
            file=sys.stderr,
        )
        return 130
