# V0114 polling: USB interval and speed

2026-10-27. Static investigation of the original readback and tested sleep-only
candidate. Internal report timing is covered separately in
[the timing analysis](v0114-polling-timing.md). These findings explain why the
implemented profiles change the descriptor interval and leave clocks alone.

## Result

The advertised interval is one byte at `0x1f36`, currently `0x0a` (10 ms).
Changing it on top of the sleep-only candidate leaves the sleep hook and
trampoline untouched. Current Linux explicitly accepts low-speed interrupt
intervals below the USB specification's 10 ms minimum; its xHCI implementation
can configure 1 ms from such a descriptor. Thus a descriptor-only low-speed
experiment is a smaller first step than changing device speed or clocks.

This does not establish that the MCU will produce fresh reports at the requested
rate, or that another host/OS will accept the nonstandard descriptor. Actual
report cadence must be measured during motion. A host poll can receive NAK
instead of a report.

The completed [internal timing analysis](v0114-polling-timing.md) found no fixed
100 Hz software throttle: endpoint completion clears the pending flag and
bypasses the timer wait, and queuing a report triggers acquisition of the next
sample. This supports starting with descriptor-only 4/2/1-ms profiles, retaining
the existing clocks, timers, and sleep fix. Do not shorten the timer fallback
preemptively: it can process and discard changed inputs while the endpoint is
busy. Sustainable throughput and optical sampling remain measurement questions;
button debounce and recovery use call counts, so their wall times may change
with workload.

## Inputs and descriptor location

- `backup-v0114/dump-1.bin`: 16,384 bytes, SHA-256
  `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1`.
- Sleep-only candidate: SHA-256
  `7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074`.
- Device and configuration descriptors occupy `[0x1f03,0x1f37)` and match
  `tools/k64325_backup.py`'s `APP_USB` with release `0x0114` exactly.
- Endpoint descriptor `[0x1f30,0x1f37)` is `07 05 81 03 04 00 0a`: endpoint
  `0x81`, interrupt IN, four-byte maximum packet, `bInterval=10`.
- GET_DESCRIPTOR's configuration branch at `0x1144..0x1157` selects code
  pointer `0x1f15` and length `0x22`. The interval is the last byte of that
  returned configuration, not an unused descriptor copy.

| `0x1f36` | Requested interval | Linux xHCI interval from descriptor | Nominal poll opportunities |
| --- | --- | --- | --- |
| `0a` (existing) | 10 ms | 8 ms | 125/s |
| `08` | 8 ms | 8 ms | 125/s |
| `04` | 4 ms | 4 ms | 250/s |
| `02` | 2 ms | 2 ms | 500/s |
| `01` | 1 ms | 1 ms | 1,000/s |

These are scheduling calculations. The tools offer 10/4/2/1 ms; the 8 ms row
explains host rounding and is not an additional generated profile.

The original application capture explicitly says
`Negotiated speed: Low Speed (1Mbps)`. This is the tool's printed label;
USB low speed is 1.5 Mbit/s. It also shows release `1.14`, four-byte endpoint
`0x81`, and interval 10.
The descriptor's `bcdUSB=2.00` is not the negotiated speed.

## Linux behavior and standards boundary

Linux source inspected on 2026-10-27:

- [USB descriptor parsing](https://github.com/torvalds/linux/blob/master/drivers/usb/core/config.c),
  `usb_parse_endpoint` sets minimum interval to 1 for interrupt
  endpoints and explicitly permit sub-10-ms low-speed intervals for
  "overclocked" devices. The code comments identify 10 ms as the official
  low-speed minimum.
- [xHCI endpoint setup](https://github.com/torvalds/linux/blob/master/drivers/usb/host/xhci-mem.c),
  `xhci_get_endpoint_interval` and `xhci_parse_frame_interval`: low-speed and
  full-speed interrupt endpoints use the same conversion. It computes
  `fls(bInterval * 8) - 1`, clamped to exponents 3–10, and schedules every
  `2^exponent` 125-us units. Therefore 10 becomes 8 ms and 1 remains 1 ms.
- [HID polling](https://github.com/torvalds/linux/blob/master/drivers/hid/usbhid/hid-core.c)
  and [URB submission](https://github.com/torvalds/linux/blob/master/drivers/usb/core/urb.c) show
  `usbhid.mousepoll` overriding the URB interval and USB core rounding
  full-/low-speed intervals down to a power of two. This override does not
  change the device descriptor or USB speed. xHCI endpoint setup reads the
  descriptor directly; do not promise that the module parameter alone changes
  the actual schedule on every controller.

Full-speed interrupt descriptors permit 1–255 ms. A full-speed implementation
is the standards-compliant way to advertise 1 ms, but Linux's explicit
low-speed exception means full-speed migration is not a prerequisite for a
Linux-only experiment. Do not confuse a descriptor request with either
guaranteed transfers or fresh sensor samples.

## Full-speed investigation: useful comparison, not a ready patch

The [SG8F7581 manufacturer page](https://www.sigmachip.com/en/keyboard-controller/sg8f7581/)
documents both USB full-speed and low-speed modes, internal USB clock recovery,
and no external crystal requirement for full-speed operation. The tested loader
uses 64-byte endpoint reports, which exceed the low-speed interrupt packet
limit. No saved bootloader negotiated-speed capture was found.

Application USB initialization at `0x251a`, called at `0x169e`, differs from
loader initialization at `0x084e`:

| Location | Bytes | Instruction |
| --- | --- | --- |
| Loader `0x0859` | `43 c1 08` | `ORL 0xc1,#0x08` |
| Application `0x2538` | `53 c1 f7` | `ANL 0xc1,#0xf7` |

Both paths clear SFR `0xc1` first. Bit 3 is a strong speed-selection candidate,
but its meaning is inferred rather than confirmed by a register manual.
Replacing the application instruction with `43 c1 08` would set that bit;
it is not enough to claim a working full-speed application.

Clock initialization differs too:

| Setting | Loader routine `0x093b` | Application routine `0x2791` |
| --- | --- | --- |
| SFR `0xbc` | `0xb1`, wait bit 1, OR `0x40`, wait bit 2 | `0x88`, wait bit 3 |
| SFR `0xbd` | `0x8c` | `0x8c` |
| SFR `0xbe` | `0x80`, wait bit 6 | `0x80`, wait bit 6 |
| SFR `0xc7` | `0xc9` | `0x89` |

Application addresses include `0x279f: 75 bc 88` and
`0x27b2: 75 c7 89`. No register-level source obtained so far resolves the
clock-selection/divider/ready-bit meanings. Changing clocks can affect timer
cadence, bit-banged sensor timing, and delay loops. Copying the loader sequence
blindly would not establish that those application timings remain correct.

Suspend/wakeup masks of `0xc1` at `0x1044`, `0x1064`, `0x1090`, `0x1093`,
`0x109d`, and `0x10a0` preserve bit 3. The recovery path writes `0xc1=0x82`
at `0x1732` and jumps through full application initialization again. Full-speed
experiments must cover both initialization and resume/reinitialization behavior.
Losing application enumeration can prevent software bootloader re-entry even
when loader bytes remain intact.

## Profile constraints

Polling profiles preserve the sleep hook/trampoline and loader/metadata bytes.
A descriptor change adds page `0x1f00` to the pages the installer may rewrite.
The host tools recognize each supported interval's exact descriptor and image
hash, including when checking application enumeration after exit. A broader
timing or clock change would require separate analysis; the descriptor-only
findings do not cover it. See the [firmware notes](../firmware.md#optional-polling-profiles)
for the implemented profile and transition rules.

## Verification performed

- Rehashed the original and sleep-only images and checked the endpoint bytes.
- Constructed interval 8/4/2/1 variants in memory only: each differed from the
  sleep-only image at exactly `0x1f36`; sleep hook/trampoline and the entire
  loader/metadata region remained identical.
- Independently restarted radare2 8051 decoding for `[0x084e,0x0871)`,
  `[0x093b,0x0963)`, `[0x251a,0x2556)`, `[0x2791,0x27b6)`.
  All 71 instructions matched the raw image and saved disassembly.
- Firmware mode/clock instructions are identical between original and
  sleep-only images. No polling variant was written to a firmware file.

These checks establish byte locations, host-code behavior, and preservation
of the existing patch. They do not establish the device's achievable report
rate or host interoperability.
