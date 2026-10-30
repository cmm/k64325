"""Read-only Linux hidraw/evdev motion capture and inspectable reduction."""

import argparse
import fcntl
import itertools
import json
import math
import os
import platform
import re
import selectors
import signal
import stat
import struct
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median, pstdev

import k64325_backup as backup
import k64325_workflow as workflow

SYS_INPUT = Path("/sys/class/input")
DEV_INPUT = Path("/dev/input")
READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
SYN_REPORT, SYN_DROPPED = 0, 3
REL_X, REL_Y, REL_WHEEL = 0, 1, 8
BUS_USB = 3
# Linux asm-generic ioctl encoding, limited to the native ABIs checked below.
EVIOCGID = 0x80084502
EVIOCSCLOCKID = 0x400445A0
INPUT_ID = struct.Struct("@HHHH")
INPUT_EVENT = struct.Struct("@llHHi")
QUIET_NS = 500_000_000
QUIET_TIMEOUT_NS = 5_000_000_000
CHECK_NS = 1_000_000_000
MAX_BYTES = 32 * 1024 * 1024
MAX_READS = 600_000
MAX_EVENTS = 600_000
# Kensington specifies a 55 mm ball for K64325 at the sources below. This
# supplies a nominal circumference, not the sensor's location on the sphere.
NOMINAL_BALL_DIAMETER_MM = 55.0
BALL_DIAMETER_SOURCES = (
    {
        "url": "https://www.kensington.com/p/products/electronic-control-solutions/trackball-products/expert-mouse-wired-trackball",
        "details": "Large 55mm ball",
    },
    {
        "url": "https://www.kensington.com/en-ca/p/products/ergonomics/ergonomic-input-devices/expert-mouse-optical-trackball2",
        "details": "SKU K64325; Trackball Diameter (mm): 55",
    },
)
TIMING_NOTE = (
    "Kernel monotonic SYN_REPORT timestamps measure host motion-event delivery, "
    "not USB bus poll opportunities or optical sensor frames. Pauses affect cadence; "
    "unchanged inputs may not report. HID arrival estimates use a separate, "
    "explicitly labeled timing source."
)
REPEAT_NOTE = (
    "Identical nonzero deltas can be fresh samples during steady quantized motion; "
    "repetitions and stream parity do not establish optical sample freshness or "
    "prove that no device-side samples were lost."
)
CALIBRATION_NOTE = (
    "Calibration uses TOTAL physical ball-surface travel past the sensor, never "
    "screen/cursor travel. Measured --surface-distance-mm takes precedence; "
    "otherwise --turns estimates distance as pi * ball diameter * turns. Estimated "
    "effective reported CPI assumes straight one-way motion and correct geometry; "
    "it is not a sensor-register CPI reading."
)
ROTATION_NOTE = (
    "Great-circle assumption: the sensor must see the full pi * D path per turn. "
    "Oblique spin or an offset rotation axis may trace a smaller circle; wobble "
    "and partial turns reduce accuracy. Model diameter alone does not locate the sensor."
)


class MeasurementError(backup.BackupError):
    """Capture refused or invalidated."""


def require_abi():
    if (
        sys.platform != "linux"
        or platform.machine() not in {"x86_64", "aarch64"}
        or sys.byteorder != "little"
        or struct.calcsize("@l") != 8
        or struct.calcsize("@P") != 8
        or INPUT_EVENT.size != 24
        or INPUT_ID.size != 8
    ):
        raise MeasurementError(
            "requires native 64-bit little-endian Linux x86_64/aarch64 input_event ABI"
        )


def positive_number(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a finite positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def seconds_argument(value):
    number = positive_number(value)
    if number > 120:
        raise argparse.ArgumentTypeError("must be at most 120 seconds")
    return number


class MeasurementParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        parsed.ball_diameter_overridden = parsed.ball_diameter_mm is not None
        if parsed.ball_diameter_overridden and parsed.turns is None:
            self.error("--ball-diameter-mm requires --turns")
        if not parsed.ball_diameter_overridden:
            parsed.ball_diameter_mm = NOMINAL_BALL_DIAMETER_MM
        return parsed


def parser():
    cli = MeasurementParser(
        description=(
            "Measure pre-compositor motion using read-only hidraw and evdev, "
            "independent of display refresh. Requires a TTY and one exact V0114 "
            "application device. Cursor movement continues. Linux native 64-bit "
            "x86_64/aarch64 only. Help opens no device."
        ),
        epilog=(
            "Timing: tools/measure-motion --seconds 10\n"
            "Rotation-based estimated CPI: tools/measure-motion --seconds 15 --turns 1\n"
            "Turns use the nominal K64325 55 mm ball diameter unless overridden. "
            f"Manufacturer source: {BALL_DIAMETER_SOURCES[0]['url']}\n"
            f"{ROTATION_NOTE}\n"
            "Rotate slowly in a fixed direction about a fixed axis: roll, do not "
            "twirl around the sensor normal. Complete exactly the declared turns, then stop.\n"
            f"{CALIBRATION_NOTE} No calibration option means no CPI estimate.\n"
            "No sudo, firmware commands, input grabbing or pointer configuration. "
            "Shared workflow and hidraw locks are advisory; close other device tools."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cli.add_argument(
        "--seconds",
        type=seconds_argument,
        default=10.0,
        help="movement window in seconds (default: 10, maximum: 120); settle tail is included",
    )
    cli.add_argument(
        "--turns",
        type=positive_number,
        help="exact TOTAL turns; estimate CPI from circumference unless measured travel is supplied",
    )
    distance = cli.add_mutually_exclusive_group()
    distance.add_argument(
        "--surface-distance-mm",
        type=positive_number,
        help="measured TOTAL physical travel at the sensor in mm, NOT screen travel; overrides turn-derived distance",
    )
    distance.add_argument(
        "--ball-diameter-mm",
        type=positive_number,
        help="ball diameter override (default: manufacturer nominal 55 mm); requires --turns; incompatible with --surface-distance-mm",
    )
    cli.add_argument(
        "--out-root", default="work", type=Path, help="output parent (default: work)"
    )
    cli.add_argument(
        "--label", default="", help="annotation only; does not verify firmware settings"
    )
    return cli


def calibration_distance(args):
    provenance = {
        "source": "not_supplied",
        "total_surface_distance_mm": None,
        "ball_diameter_mm": None,
        "diameter_origin": None,
        "turns": None,
        "formula": None,
        "manufacturer_reference": None,
        "assumptions": None,
        "issue": None,
    }
    if args.surface_distance_mm is not None:
        provenance.update(
            source="measured_surface_travel",
            total_surface_distance_mm=args.surface_distance_mm,
        )
    elif args.turns is not None:
        # Balance the factors so a large diameter and small turn count do not
        # overflow an intermediate while their total distance is representable.
        factors = sorted((math.pi, args.ball_diameter_mm, args.turns))
        distance = factors[0] * factors[2] * factors[1]
        representable = math.isfinite(distance) and distance > 0
        provenance.update(
            source="estimated_circumference",
            total_surface_distance_mm=distance if representable else None,
            ball_diameter_mm=args.ball_diameter_mm,
            diameter_origin="user_override"
            if args.ball_diameter_overridden
            else "manufacturer_nominal",
            turns=args.turns,
            formula="pi * ball_diameter_mm * turns",
            manufacturer_reference={
                "model": "K64325",
                "nominal_ball_diameter_mm": NOMINAL_BALL_DIAMETER_MM,
                "sources": list(BALL_DIAMETER_SOURCES),
            },
            assumptions=ROTATION_NOTE,
            issue=None
            if representable
            else "rotation-derived distance is outside positive finite floating-point range",
        )
    return provenance


def distance_description(provenance):
    source = provenance["source"]
    if source == "not_supplied":
        return "Calibration distance: not supplied."
    distance = provenance["total_surface_distance_mm"]
    amount = f"{distance:g} mm" if distance is not None else "outside numeric range"
    if source == "measured_surface_travel":
        return f"Calibration distance: {amount}; measured surface travel."
    origin = (
        "K64325 manufacturer nominal"
        if provenance["diameter_origin"] == "manufacturer_nominal"
        else "user override"
    )
    return (
        f"Calibration distance: {amount}; estimated circumference pi * "
        f"{provenance['ball_diameter_mm']:g} mm ({origin}) * {provenance['turns']:g} turns."
    )


def bitmap(path):
    raw = backup.read_sysfs(path, 4096).strip()
    if re.fullmatch(rb"[0-9a-fA-F]{1,16}(?: [0-9a-fA-F]{1,16}){0,15}", raw) is None:
        raise MeasurementError(f"invalid native 64-bit sysfs bitmap: {path}")
    value = 0
    for word in raw.split():
        value = (value << 64) | int(word, 16)
    return value


@dataclass(frozen=True)
class EventNode:
    device: str
    class_path: str
    input_path: str
    hid_path: str
    path_inodes: tuple
    device_number: tuple
    input_id: tuple
    event_bits: int
    rel_bits: int


def inspect_event(entry, node):
    input_path = (entry / "device").resolve(strict=True)
    hid_path = Path(node.hid_path)
    if hid_path not in input_path.parents:
        return None
    class_path = entry.resolve(strict=True)
    if input_path not in class_path.parents:
        raise MeasurementError(f"event/input ancestry disagreement: {entry}")
    raw = backup.read_sysfs(entry / "dev", 32).strip()
    if re.fullmatch(rb"[0-9]+:[0-9]+", raw) is None:
        raise MeasurementError(f"invalid event device number: {entry}")
    identity = tuple(
        backup.sysfs_hex(input_path / "id" / key)
        for key in ("bustype", "vendor", "product", "version")
    )
    # Linux usbhid sets the input version from bcdHID, not bcdDevice:
    # https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hid/usbhid/hid-core.c?h=v6.12
    # In the validated USB layout, bcdHID follows the 18-byte device,
    # 9-byte configuration, 9-byte interface, and 2-byte HID headers.
    hid_version = int.from_bytes(node.usb_descriptor[38:40], "little")
    expected = (BUS_USB, node.vid, node.pid, hid_version)
    if identity != expected:
        actual_text = ":".join(f"{value:04x}" for value in identity)
        expected_text = ":".join(f"{value:04x}" for value in expected)
        raise MeasurementError(
            f"input/USB identity disagreement: {entry}; "
            f"bus:vendor:product:HID-version={actual_text}, expected {expected_text} "
            f"(USB firmware release {node.release:04x} is checked separately)"
        )
    event_bits = bitmap(input_path / "capabilities/ev")
    rel_bits = bitmap(input_path / "capabilities/rel")
    if (
        event_bits & ((1 << EV_SYN) | (1 << EV_REL)) != ((1 << EV_SYN) | (1 << EV_REL))
        or rel_bits & 3 != 3
    ):
        raise MeasurementError(f"associated input lacks EV_REL/REL_X/REL_Y: {entry}")
    paths = (class_path, input_path, hid_path)
    return EventNode(
        str(DEV_INPUT / entry.name),
        str(class_path),
        str(input_path),
        str(hid_path),
        tuple(workflow.inode(path.stat()) for path in paths),
        tuple(int(part) for part in raw.split(b":")),
        identity,
        event_bits,
        rel_bits,
    )


def discover_event(node):
    found = []
    # Listing/resolution errors must not silently hide a second associated node.
    for entry in sorted(SYS_INPUT.iterdir()):
        if re.fullmatch(r"event[0-9]+", entry.name) is not None:
            event = inspect_event(entry, node)
            if event is not None:
                found.append(event)
    if len(found) != 1:
        raise MeasurementError(
            f"requires exactly one event node under the same HID parent; found {len(found)}"
        )
    return found[0]


def device_stat(path, number):
    path = Path(path)
    with workflow.open_directory(path.parent) as directory_fd:
        info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    verify_character(info, number)
    return info


def verify_character(info, number):
    if (
        not stat.S_ISCHR(info.st_mode)
        or (os.major(info.st_rdev), os.minor(info.st_rdev)) != number
    ):
        raise MeasurementError("device node is not the expected character device")


def verify_fd(fd, snapshot, number):
    info = os.fstat(fd)
    verify_character(info, number)
    if workflow.inode(info) != workflow.inode(snapshot):
        raise MeasurementError("opened device node was replaced")


def open_readonly(path):
    path = Path(path)
    with workflow.open_directory(path.parent) as directory_fd:
        return os.open(path.name, READ_FLAGS, dir_fd=directory_fd)


class MeasurementBinding:
    def __init__(self, lock):
        self.lock = lock
        self.stack = ExitStack()
        self.fds = {}
        self.usb, self.hid = workflow.discover()
        if workflow.node_mode(self.hid) != "application":
            raise MeasurementError(
                "measurement requires V0114 application mode, not boot"
            )
        self.event = discover_event(self.hid)
        self.snapshots = {
            kind: device_stat(node.device, node.device_number)
            for kind, node in (("hidraw", self.hid), ("evdev", self.event))
        }

    def check(self):
        self.lock.check()
        usb, hid = workflow.discover()
        if (usb, hid) != (self.usb, self.hid):
            raise MeasurementError("USB/HID singleton, identity or path changed")
        if discover_event(hid) != self.event:
            raise MeasurementError("associated input node, identity or path changed")
        for kind, node in (("hidraw", self.hid), ("evdev", self.event)):
            current = device_stat(node.device, node.device_number)
            if workflow.inode(current) != workflow.inode(self.snapshots[kind]):
                raise MeasurementError(f"{kind} device path was replaced")
            if kind in self.fds:
                verify_fd(self.fds[kind], self.snapshots[kind], node.device_number)

    def __enter__(self):
        try:
            self.check()
            for kind, node in (("hidraw", self.hid), ("evdev", self.event)):
                fd = open_readonly(node.device)
                self.stack.callback(os.close, fd)
                self.fds[kind] = fd
                verify_fd(fd, self.snapshots[kind], node.device_number)
                if kind == "hidraw":
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise MeasurementError(
                            "hidraw is locked by another tool"
                        ) from error
            identity = bytearray(INPUT_ID.size)
            fcntl.ioctl(self.fds["evdev"], EVIOCGID, identity, True)
            if INPUT_ID.unpack(identity) != self.event.input_id:
                raise MeasurementError("opened evdev EVIOCGID disagrees with USB/sysfs")
            # Reader clock selection only: this does not send a command to hardware.
            fcntl.ioctl(
                self.fds["evdev"],
                EVIOCSCLOCKID,
                struct.pack("@i", time.CLOCK_MONOTONIC),
            )
            self.check()
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def metadata(self):
        interval = self.hid.usb_descriptor[-1]
        return {
            "usb_path": self.hid.usb_path,
            "hidraw": self.hid.device,
            "evdev": self.event.device,
            "identity": self.hid.identity(),
            "input": asdict(self.event),
            "descriptor_bInterval_ms": interval,
            "descriptor_nominal_reciprocal_hz": 1000 / interval,
            "descriptor_note": (
                "Descriptor interval is not an observed poll or optical sample rate. "
                "Linux xHCI can schedule a 10 ms interval at 8 ms."
            ),
        }


@dataclass(slots=True)
class RawRead:
    arrival_ns: int
    phase: str
    data: bytes

    def record(self):
        return {
            "arrival_monotonic_ns": self.arrival_ns,
            "phase": self.phase,
            "data_hex": self.data.hex(),
        }


@dataclass(slots=True)
class Frame:
    kernel_ns: int
    phase: str
    dx: int
    dy: int
    wheel: int
    other_events: int


def hid_report(data):
    if len(data) != 4:
        raise MeasurementError(
            f"hidraw report must be exactly 4 bytes, got {len(data)}"
        )
    buttons, dx, dy, wheel = struct.unpack("Bbbb", data)
    return buttons & 31, dx, dy, wheel


class Collector:
    """Bounded, fair dual-FD capture; no disk writes or per-event sysfs scans."""

    def __init__(self, hidraw_fd, evdev_fd, *, clock=time.monotonic_ns):
        self.fds = {"hidraw": hidraw_fd, "evdev": evdev_fd}
        self.clock = clock
        self.hidraw = []
        self.evdev = []
        self.frames = []
        self.buffer = b""
        self.pending = [0, 0, 0, 0]
        self.pending_events = 0
        self.last_kernel_ns = None
        self.event_count = 0
        self.byte_count = 0
        self.read_count = 0
        self.phase = "arming"
        self.collection_begin_ns = clock()
        self.start_ns = None
        self.stop_ns = None
        self.end_ns = None
        self.last_activity_ns = self.collection_begin_ns
        self.activity_count = 0
        self.last_buttons = 0
        self.complete = False

    def feed(self, kind, data, arrival_ns=None):
        arrival_ns = self.clock() if arrival_ns is None else arrival_ns
        raw = RawRead(arrival_ns, self.phase, data)
        getattr(self, kind).append(raw)
        self.byte_count += len(data)
        self.read_count += 1
        if self.byte_count > MAX_BYTES or self.read_count > MAX_READS:
            raise MeasurementError("capture byte/read resource cap exceeded")
        if not data:
            suffix = (
                " (truncated input_event)" if kind == "evdev" and self.buffer else ""
            )
            raise MeasurementError(f"{kind} EOF/disconnect{suffix}")
        if kind == "hidraw":
            buttons, dx, dy, wheel = hid_report(data)
            if dx or dy or wheel or buttons != self.last_buttons:
                self.last_activity_ns = arrival_ns
                self.activity_count += 1
            self.last_buttons = buttons
            return
        self.buffer += data
        offset = 0
        try:
            while len(self.buffer) - offset >= INPUT_EVENT.size:
                sec, usec, event_type, code, value = INPUT_EVENT.unpack_from(
                    self.buffer, offset
                )
                offset += INPUT_EVENT.size
                self.event_count += 1
                if self.event_count > MAX_EVENTS:
                    raise MeasurementError("capture input-event resource cap exceeded")
                if sec < 0 or not 0 <= usec < 1_000_000:
                    raise MeasurementError("malformed input_event timestamp")
                if (
                    event_type > 0x1F
                    or (event_type == EV_REL and code > 0x0F)
                    or (event_type == EV_SYN and code > 0x0F)
                    or (event_type == EV_SYN and code == SYN_REPORT and value != 0)
                ):
                    raise MeasurementError(
                        "malformed input_event type, code or SYN_REPORT"
                    )
                kernel_ns = sec * 1_000_000_000 + usec * 1000
                if self.last_kernel_ns is not None and kernel_ns < self.last_kernel_ns:
                    raise MeasurementError("evdev kernel clock regressed")
                self.last_kernel_ns = kernel_ns
                if event_type == EV_SYN and code == SYN_DROPPED:
                    raise MeasurementError("evdev SYN_DROPPED: events were lost")
                if event_type == EV_SYN and code == SYN_REPORT:
                    self.frames.append(Frame(kernel_ns, self.phase, *self.pending))
                    self.pending = [0, 0, 0, 0]
                    self.pending_events = 0
                else:
                    self.pending_events += 1
                    if event_type != EV_REL or value:
                        self.last_activity_ns = arrival_ns
                        self.activity_count += 1
                    if event_type == EV_REL and code in (REL_X, REL_Y, REL_WHEEL):
                        index = {REL_X: 0, REL_Y: 1, REL_WHEEL: 2}[code]
                        self.pending[index] += value
                    else:
                        self.pending[3] += 1
        finally:
            self.buffer = self.buffer[offset:]

    def require_boundary(self):
        if self.buffer or self.pending_events:
            raise MeasurementError(
                "quiet boundary has truncated records or unfinished frame"
            )

    def read_ready(self, selector, timeout):
        # One bounded read per ready FD per pass prevents either stream starving.
        before = self.read_count
        for key, _ in selector.select(timeout):
            try:
                data = os.read(key.fd, 4096 if key.data == "hidraw" else 24 * 64)
            except BlockingIOError:
                continue
            self.feed(key.data, data)
        return self.read_count != before

    def drain_ready(self, selector, deadline_ns):
        while True:
            if self.clock() >= deadline_ns:
                raise MeasurementError("queue drain exceeded quiet-boundary time limit")
            if not self.read_ready(selector, 0):
                self.require_boundary()
                return

    def _wait(self, selector, deadline_ns, quiet, check, next_check):
        while True:
            now = self.clock()
            # Always sample readiness before declaring quiet, even after a slow check.
            received = self.read_ready(selector, 0)
            now = self.clock()
            # Stationary reports need not be suppressed by the device. They do
            # not disturb motion alignment, but the queues must still be drained.
            if quiet and not received and now - self.last_activity_ns >= QUIET_NS:
                self.require_boundary()
                return next_check
            if now >= deadline_ns:
                if quiet:
                    raise MeasurementError("no bounded quiet boundary within 5 seconds")
                return next_check
            if now >= next_check:
                check()
                next_check = self.clock() + CHECK_NS
                continue
            until = min(deadline_ns, next_check, now + 50_000_000)
            if quiet:
                until = min(until, self.last_activity_ns + QUIET_NS)
            self.read_ready(selector, max(0, until - now) / 1_000_000_000)

    def run(
        self,
        seconds,
        check,
        *,
        announce=print,
        selector_factory=selectors.DefaultSelector,
    ):
        with selector_factory() as selector:
            for kind, fd in self.fds.items():
                selector.register(fd, selectors.EVENT_READ, kind)
            announce("Waiting for the ball to settle...")
            next_check = self.clock() + CHECK_NS
            arming_deadline = self.clock() + QUIET_TIMEOUT_NS
            self._wait(
                selector,
                arming_deadline,
                True,
                check,
                next_check,
            )
            check()
            # A check can take time; re-establish an empty queue after it.
            self.drain_ready(selector, arming_deadline)
            if self.clock() - self.last_activity_ns < QUIET_NS:
                raise MeasurementError(
                    "motion resumed while arming; retry while holding still"
                )
            self.phase = "capture"
            self.start_ns = self.clock()
            announce("Move now", flush=True)
            next_check = self._wait(
                selector,
                self.start_ns + max(1, int(seconds * 1_000_000_000)),
                False,
                check,
                self.clock() + CHECK_NS,
            )
            self.stop_ns = self.clock()
            announce("Stop moving", flush=True)
            self.phase = "settle"
            # Include all settle-tail reports/frames in counts and cadence.
            self.last_activity_ns = self.stop_ns
            settle_deadline = self.stop_ns + QUIET_TIMEOUT_NS
            self._wait(
                selector,
                settle_deadline,
                True,
                check,
                next_check,
            )
            before = self.activity_count
            check()
            self.drain_ready(selector, settle_deadline)
            if self.activity_count != before:
                raise MeasurementError("activity resumed during final identity check")
            self.end_ns = self.clock()
            self.complete = True


def ratio(numerator, denominator):
    if not denominator:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def percentile(values, fraction):
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def cadence(stamps, span_ns):
    intervals = sorted((b - a) / 1_000_000 for a, b in itertools.pairwise(stamps))
    bins = [0, 1.5, 3, 6, 12, 20, 50, math.inf]
    names = ["0", "(0,1.5]", "(1.5,3]", "(3,6]", "(6,12]", "(12,20]", "(20,50]", ">50"]
    histogram = dict.fromkeys(names, 0)
    for interval in intervals:
        for edge, name in zip(bins, names):
            if interval <= edge:
                histogram[name] += 1
                break
    mean = sum(intervals) / len(intervals) if intervals else None
    middle = median(intervals) if intervals else None
    spread = pstdev(intervals) if intervals else None
    return {
        "motion_frame_count": len(stamps),
        "first_monotonic_ns": stamps[0] if stamps else None,
        "last_monotonic_ns": stamps[-1] if stamps else None,
        "active_span_hz": (
            ratio((len(stamps) - 1) * 1_000_000_000, stamps[-1] - stamps[0])
            if len(stamps) >= 2
            else None
        ),
        "median_interval_hz": ratio(1000, middle),
        "observation_window_hz": ratio(len(stamps) * 1_000_000_000, span_ns),
        "interval_ms": {
            "count": len(intervals),
            "min": intervals[0] if intervals else None,
            "median": middle,
            "p95": percentile(intervals, 0.95),
            "p99": percentile(intervals, 0.99),
            "max": intervals[-1] if intervals else None,
            "mean": mean,
            "population_stddev": spread,
            "coefficient_of_variation": ratio(spread, mean)
            if spread is not None
            else None,
            "histogram": histogram,
            "gaps_over_1_5x_median": (
                sum(interval > middle * 1.5 for interval in intervals)
                if middle
                else None
            ),
            "percentile_method": "linear interpolation between ordered intervals",
        },
    }


def reduce_capture(capture, args, device, *, failure=None, interrupted=False):
    distance = calibration_distance(args)
    selected = [raw for raw in capture.hidraw if raw.phase != "arming"]
    reports = [hid_report(raw.data) for raw in selected if len(raw.data) == 4]
    motions = [(dx, dy) for _, dx, dy, _ in reports if dx or dy]
    frames = [frame for frame in capture.frames if frame.phase != "arming"]
    motion_frames = [frame for frame in frames if frame.dx or frame.dy]
    event_motions = [(frame.dx, frame.dy) for frame in motion_frames]
    evdev_available = any(raw.phase != "arming" and raw.data for raw in capture.evdev)
    parity = motions == event_motions if evdev_available else None
    parity_status = (
        "unavailable" if parity is None else "matched" if parity else "mismatch"
    )
    mismatch = next(
        (index for index, (a, b) in enumerate(zip(motions, event_motions)) if a != b),
        None,
    )
    if mismatch is None and parity is False:
        mismatch = min(len(motions), len(event_motions))
    issues = [failure] if failure else []
    if not capture.complete:
        issues.append("capture did not reach checked quiet completion")
    if parity is False:
        issues.append("ordered nonzero hidraw/evdev motion sequence mismatch")
    valid = not issues and not interrupted
    dx = sum(x for x, _ in motions)
    dy = sum(y for _, y in motions)
    net = math.hypot(dx, dy)
    path = sum(math.hypot(x, y) for x, y in motions)
    projections = [x * dx + y * dy for x, y in motions]
    reversed_count = sum(value < 0 for value in projections)
    backwards = sum(-value for value in projections if value < 0)
    projected_path = sum(abs(value) for value in projections)
    net_path = ratio(net, path)
    reverse_fraction = ratio(backwards, projected_path)
    clipped = sum(any(abs(value) >= 127 for value in (x, y)) for x, y in motions)
    wheel_clipped = sum(abs(wheel) >= 127 for _, _, _, wheel in reports)
    identical = sum(a == b for a, b in itertools.pairwise(motions))
    longest = run = 1 if motions else 0
    for a, b in itertools.pairwise(motions):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    end_ns = capture.end_ns if capture.end_ns is not None else capture.clock()
    span_ns = end_ns - capture.start_ns if capture.start_ns is not None else None
    if evdev_available:
        stamps = [frame.kernel_ns for frame in motion_frames]
        timing_source = "evdev_kernel"
        timing_note = TIMING_NOTE
    else:
        stamps = [
            raw.arrival_ns
            for raw in selected
            if len(raw.data) == 4 and any(hid_report(raw.data)[1:3])
        ]
        timing_source = "hidraw_arrival"
        timing_note = (
            "Userspace HID arrival intervals estimate report delivery cadence. "
            "Reader scheduling and queued reports affect interval spread. "
            "The kernel input-event stream supplied no capture data."
        )
    timing = cadence(stamps, span_ns)
    timing.update(
        diagnostic_only=not valid,
        source=timing_source,
        quality="kernel_delivery" if evdev_available else "arrival_estimate",
        note=timing_note,
    )
    if not evdev_available:
        threshold_ms = device["descriptor_bInterval_ms"] / 2
        timing["read_batching"] = {
            "short_gap_threshold_ms": threshold_ms,
            "short_gap_count": sum(
                (b - a) / 1_000_000 < threshold_ms
                for a, b in itertools.pairwise(stamps)
            ),
            "note": "Closely spaced reads can reflect queued delivery; they do not establish faster device polling.",
        }
    result = {
        "schema_version": 2,
        "status": "valid" if valid else "incomplete" if interrupted else "invalid",
        "usable_metrics": valid,
        "issues": issues,
        "options": {
            "seconds": args.seconds,
            "turns": args.turns,
            "surface_distance_mm": args.surface_distance_mm,
            "ball_diameter_mm": args.ball_diameter_mm,
            "ball_diameter_overridden": args.ball_diameter_overridden,
            "label": args.label,
            "label_note": "user annotation only; not proof of firmware settings",
        },
        "device": device,
        "capture": {
            "collection_begin_monotonic_ns": capture.collection_begin_ns,
            "start_monotonic_ns": capture.start_ns,
            "stop_prompt_monotonic_ns": capture.stop_ns,
            "end_monotonic_ns": capture.end_ns,
            "observation_span_seconds": span_ns / 1_000_000_000
            if span_ns is not None
            else None,
            "counts_include_settle_tail": True,
            "phase_note": "arming is excluded; capture and settle are included in all metrics",
            "quiet_seconds": QUIET_NS / 1_000_000_000,
            "quiet_note": "no motion/button/wheel activity, with both queues drained; stationary zero reports do not reset quiet",
            "quiet_timeout_seconds": QUIET_TIMEOUT_NS / 1_000_000_000,
            "limits": {
                "bytes": MAX_BYTES,
                "reads": MAX_READS,
                "input_events": MAX_EVENTS,
            },
            "all_phases_bytes": capture.byte_count,
            "all_phases_reads": capture.read_count,
            "all_phases_input_events": capture.event_count,
        },
        "raw_format": {
            "hidraw": "one read/report per JSONL row; data_hex is exact unnumbered report bytes",
            "evdev": (
                "one read per JSONL row; concatenate data_hex within each phase and "
                "decode native little-endian 64-bit Linux input_event (@llHHi, 24 bytes); "
                "assemble EV_REL values until SYN_REPORT, whose timestamp times the frame"
            ),
            "arrival_monotonic_ns": "userspace ARRIVAL timestamp, not kernel/sensor time",
        },
        "parity": {
            "status": parity_status,
            "equal_ordered_nonzero_motion": parity,
            "hidraw_motion_count": len(motions),
            "evdev_motion_count": len(event_motions),
            "first_mismatch_index": mismatch,
            "hidraw_at_mismatch": list(motions[mismatch])
            if mismatch is not None and mismatch < len(motions)
            else None,
            "evdev_at_mismatch": list(event_motions[mismatch])
            if mismatch is not None and mismatch < len(event_motions)
            else None,
            "reason": "kernel input events unavailable" if parity is None else None,
            "note": "zero-motion reports/frames omitted only for this comparison; no deduplication",
        },
        "motion": {
            "diagnostic_only": not valid,
            "raw_report_count": len(reports),
            "nonzero_motion_reports": len(motions),
            "zero_motion_reports": len(reports) - len(motions),
            "unique_dx_dy_tuples": len({(x, y) for _, x, y, _ in reports}),
            "unique_nonzero_dx_dy_tuples": len(set(motions)),
            "adjacent_identical_nonzero_fraction": ratio(identical, len(motions) - 1)
            if len(motions) > 1
            else None,
            "longest_identical_nonzero_run": longest,
            "repeat_note": REPEAT_NOTE,
            "net_dx": dx,
            "net_dy": dy,
            "net_vector_counts": net,
            "sum_absolute_dx": sum(abs(x) for x, _ in motions),
            "sum_absolute_dy": sum(abs(y) for _, y in motions),
            "sum_frame_euclidean_counts": path,
            "net_path_ratio": net_path,
            "opposed_to_net_motion_frames": reversed_count,
            "backward_projection_fraction": reverse_fraction,
            "x_sign_reversals": sign_reversals([x for x, _ in motions]),
            "y_sign_reversals": sign_reversals([y for _, y in motions]),
            "cross_axis_net_ratio": ratio(min(abs(dx), abs(dy)), max(abs(dx), abs(dy))),
            "possible_axis_clipping_reports": clipped,
            "possible_wheel_clipping_reports": wheel_clipped,
            "unexpected_minus_128_axes": sum(
                value == -128 for _, x, y, _ in reports for value in (x, y)
            ),
            "unexpected_minus_128_wheels": sum(w == -128 for _, _, _, w in reports),
            "clipping_note": "values +/-127 may saturate; -128 is outside the descriptor logical range",
            "button_active_reports": sum(bool(buttons) for buttons, _, _, _ in reports),
            "button_state_changes": sum(
                a[0] != b[0] for a, b in itertools.pairwise(reports)
            ),
            "wheel_reports": sum(bool(wheel) for _, _, _, wheel in reports),
            "wheel_net": sum(wheel for _, _, _, wheel in reports),
            "wheel_absolute": sum(abs(wheel) for _, _, _, wheel in reports),
            "evdev_wheel_net": sum(frame.wheel for frame in frames),
            "evdev_other_events": sum(frame.other_events for frame in frames),
            "evdev_nonmotion_frames": len(frames) - len(motion_frames),
        },
        "timing": timing,
        "counts_per_turn": None,
        "calibration": {
            "estimated_effective_reported_cpi": None,
            "motion_cross_check": parity_status,
            "distance_provenance": distance,
            "note": CALIBRATION_NOTE,
            "straight_one_way_criterion": "net/path >= 0.95 and backward projection fraction <= 0.01",
            "suppressed_reasons": [],
        },
    }
    if args.turns is not None and valid:
        result["counts_per_turn"] = {
            "net_vector_magnitude": ratio(net, args.turns),
            "net_dx": ratio(dx, args.turns),
            "net_dy": ratio(dy, args.turns),
            "note": "declared total turns; null numbers exceed floating-point range",
        }
    reasons = result["calibration"]["suppressed_reasons"]
    if distance["source"] == "not_supplied":
        reasons.append(
            "neither measured surface distance nor turns supplied; screen travel is never calibration"
        )
    if distance["issue"]:
        reasons.append(distance["issue"])
    if not valid:
        reasons.append("capture is not valid")
    if not net:
        reasons.append("no nonzero net motion")
    if clipped or wheel_clipped:
        reasons.append("possible clipping/saturation")
    if net and (net_path < 0.95 or reverse_fraction > 0.01):
        reasons.append("motion fails straight one-way calibration criterion")
    if not reasons:
        cpi = ratio(net * 25.4, distance["total_surface_distance_mm"])
        if cpi is None:
            reasons.append("CPI exceeds floating-point range")
        else:
            result["calibration"]["estimated_effective_reported_cpi"] = cpi
    return result


def sign_reversals(values):
    nonzero = [value for value in values if value]
    return sum((a < 0) != (b < 0) for a, b in itertools.pairwise(nonzero))


class MeasurementFiles:
    def __init__(self, root):
        self.directory = workflow.fresh_directory(root, "measure")
        self.stack = ExitStack()

    def __enter__(self):
        self.fd = self.stack.enter_context(workflow.open_directory(self.directory))
        self.snapshot = workflow.inode(os.fstat(self.fd))
        return self

    def check(self):
        with workflow.open_directory(self.directory) as fd:
            if workflow.inode(os.fstat(fd)) != self.snapshot:
                raise MeasurementError("measurement directory was replaced")

    def write(self, name, records):
        self.check()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | workflow.FILE_FLAGS
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            for record in records:
                output.write(
                    (
                        json.dumps(record, allow_nan=False, sort_keys=True) + "\n"
                    ).encode()
                )
            output.flush()
            os.fsync(output.fileno())
        self.check()

    def save(self, capture, summary):
        # Capture has ended; disk I/O cannot perturb the measured cadence.
        self.write("hidraw.jsonl", (raw.record() for raw in capture.hidraw))
        self.write("evdev.jsonl", (raw.record() for raw in capture.evdev))
        self.write("summary.json", [summary])
        os.fsync(self.fd)

    def __exit__(self, *exc):
        self.stack.close()


def instructions(args, metadata):
    print(f"Device: {metadata['hidraw']} + {metadata['evdev']}")
    interval = metadata["descriptor_bInterval_ms"]
    print(
        f"USB interval request: {interval} ms"
        + (
            "; Linux xHCI can schedule this at 8 ms (125 Hz)."
            if interval == 10
            else "."
        )
    )
    if args.turns is not None or args.surface_distance_mm is not None:
        targets = []
        if args.turns is not None:
            targets.append(f"{args.turns:g} complete turns")
        if args.surface_distance_mm is not None:
            targets.append(f"{args.surface_distance_mm:g} mm of surface travel")
        print(
            f"After 'Move now', roll slowly in one direction about a fixed axis for "
            f"{' and '.join(targets)}, then hold still. Finish within {args.seconds:g} s."
        )
        distance = calibration_distance(args)
        print(distance_description(distance))
        if distance["source"] == "estimated_circumference":
            print(
                "The CPI estimate assumes a full circumference passes the sensor each turn."
            )
    else:
        print(f"After 'Move now', move the ball steadily for {args.seconds:g} seconds.")


def print_summary(summary, directory):
    parity = summary["parity"]
    timing = summary["timing"]
    motion = summary["motion"]
    if not summary["usable_metrics"]:
        print("Capture check failed; measurements below are unverified.")
        for issue in summary["issues"]:
            print(f"  {issue}")
    print(
        f"Motion reports: {parity['hidraw_motion_count']} HID; "
        f"{parity['evdev_motion_count']} kernel input."
    )
    if parity["status"] == "mismatch":
        print(
            f"First mismatch at report {parity['first_mismatch_index']}: "
            f"HID {parity['hidraw_at_mismatch']}, input {parity['evdev_at_mismatch']}."
        )
        print("Repeat a shorter capture to check whether the mismatch recurs.")
    if timing["source"] == "hidraw_arrival":
        print("Timing: approximate HID arrival times; kernel input events unavailable.")
        event_device = summary["device"].get("evdev")
        if motion["nonzero_motion_reports"] and event_device:
            print(
                "To investigate input access, list other readers: "
                f"sudo fuser -v {event_device}"
            )
    else:
        print("Timing: kernel input-event timestamps.")
    typical = timing["median_interval_hz"]
    average = timing["active_span_hz"]
    if typical is not None and average is not None:
        print(
            f"Typical cadence: {typical:.1f} Hz; average across motion span: {average:.1f} Hz."
        )
    intervals = timing["interval_ms"]
    if intervals["median"] is not None:
        print(
            f"Intervals: median {intervals['median']:.2f} ms, "
            f"p95 {intervals['p95']:.2f} ms, longest {intervals['max']:.2f} ms."
        )
    batching = timing.get("read_batching", {})
    if batching.get("short_gap_count"):
        print(
            f"{batching['short_gap_count']} reads were less than "
            f"{batching['short_gap_threshold_ms']:g} ms apart; reader scheduling affects this timing."
        )
    if not motion["nonzero_motion_reports"]:
        print("No movement recorded. Move the ball during the next capture window.")
    else:
        print(f"Net movement counts: X {motion['net_dx']}, Y {motion['net_dy']}.")
        repeated = motion["adjacent_identical_nonzero_fraction"]
        if repeated is not None:
            print(
                f"Consecutive equal deltas: {repeated:.1%}; "
                f"longest run {motion['longest_identical_nonzero_run']} reports. "
                "Equal deltas can occur during steady motion."
            )
    if summary["counts_per_turn"] is not None:
        turns = summary["counts_per_turn"]
        print(
            f"Counts/turn: magnitude {turns['net_vector_magnitude']}, "
            f"X {turns['net_dx']}, Y {turns['net_dy']}."
        )
    distance = summary["calibration"]["distance_provenance"]
    if distance["source"] != "not_supplied":
        cpi = summary["calibration"]["estimated_effective_reported_cpi"]
        if cpi is not None:
            print(f"Estimated effective CPI: {cpi:.1f}.")
        else:
            print(
                "CPI unavailable: "
                + "; ".join(summary["calibration"]["suppressed_reasons"])
            )
    print(f"Results: {directory} (summary.json and raw captures)")


def measure(args):
    require_abi()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise MeasurementError("interactive stdin and stdout TTYs are required")
    root = workflow.directory_path(args.out_root, create=True)
    with workflow.WorkflowLock() as lock, MeasurementBinding(lock) as binding:
        metadata = binding.metadata()
        instructions(args, metadata)
        input("Hold still and press Enter to arm: ")
        binding.check()
        with MeasurementFiles(root) as files:
            print(f"Run directory: {files.directory}", flush=True)
            capture = Collector(binding.fds["hidraw"], binding.fds["evdev"])
            failure = None
            interrupted = False
            try:
                capture.run(args.seconds, binding.check)
            except KeyboardInterrupt:
                failure = "interrupted by user"
                interrupted = True
            except (OSError, backup.BackupError) as error:
                failure = f"{type(error).__name__}: {error}"
            finally:
                if not capture.complete:
                    capture.end_ns = capture.clock()
                # A second Ctrl-C must not truncate the bounded partial-data save.
                old_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    summary = reduce_capture(
                        capture,
                        args,
                        metadata,
                        failure=failure,
                        interrupted=interrupted,
                    )
                    files.save(capture, summary)
                finally:
                    signal.signal(signal.SIGINT, old_handler)
            print_summary(summary, files.directory)
            return 130 if interrupted else 0 if summary["usable_metrics"] else 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return measure(args)
    except (OSError, backup.BackupError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Interrupted; no device commands sent.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
