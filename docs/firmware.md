# Firmware and protocol notes

These notes describe the hash-pinned V0114 image supported by the tools. USB
release `0114` is an advertised version field, not proof of identical code.
Addresses below are byte offsets in the 16 KiB flash image; range ends are
exclusive.

Detailed analysis:

- [Sensor initialization, sleep, and recovery](analysis/v0114-sensor-sleep.md)
- [USB interval and speed selection](analysis/v0114-polling-usb.md)
- [Internal report timing](analysis/v0114-polling-timing.md)
- [CPI instruction-state effects](analysis/v0114-cpi.md)
- [Software integrity and vendor packaging](analysis/v0114-integrity-software.md)

## Identification and provenance

| Object | SHA-256 |
| --- | --- |
| Original, after bootloader entry | `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1` |
| Default sleep-only image | `7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074` |
| Loader bytes `[0x0000,0x0b00)` | `5522a686f9c1d1b75a962d36896a022ddda431f565f2cf8ed07f6616d377b6a5` |

The original was obtained as two matching full readbacks from a wired K64325
advertising `047d:1020`, `bcdDevice=0114`. The second read used reverse chunk
order. Its loader bytes match the loader in Kensington's V0116 update image.
That comparison does not make the applications interchangeable: V0114 drives
sensor data on P0.1; the analyzed V0116 drives it on P0.2.

The tested controller is marked SG8F7581. The sensor-board marking
`MS-6070U-3205_Sensor` points to the PAW3205 family, but the sensor package/suffix
was not identified. The patch's register interpretation comes from the
[PAW3205DB-TJ3T V0.2 specification](https://github.com/Li-Dongze/paw3205_mouse/blob/main/PAW3205DB-TJ3T_SPEC_V0.2.pdf).

## Sensor change

The firmware's common initializer at `0x21fd` resets the sensor with `86 80`,
reads its ID, and writes configuration `86 00` or `86 01` for accepted IDs
`30` or `31`. It does not program the sensor's sleep settings. Initialization,
fault retry, and post-suspend reinitialization share this path.

On the cited sensor map:

- `Operation_Mode`, register `0x05`, resets to `0xb8`.
- Bit 3 enables automatic sleep2. Clearing it gives `0xb0`, retaining sleep1,
  adaptive frame rate, LED-shutter control, and reserved bits `[6:5]=01`.
- The default sleep2 entry delay is 20,480 ms after sleep1; default sleep1 entry
  is 256 ms. These are sensor settings, not a host USB autosuspend timer.
- Register `0x05` does not require the write-protection unlock for higher
  registers.

The patch appends the sensor transaction `85 b0` after successful normal
configuration. It changes 23 image bytes:

| Range | Original | Patched |
| --- | --- | --- |
| `[0x2262,0x2265)` | `12 28 45` | `02 2e 00` |
| `[0x2e00,0x2e14)` | Twenty `ff` bytes | `12 28 45 c0 d0 7f 85 12 28 45 7f b0 12 28 45 d0 d0 02 22 65` |

The new code executes the displaced configuration-data transmit, saves PSW,
sends the new address/data pair through the existing transmitter at `0x2845`,
restores PSW, and jumps to `0x2265`. The ready flag is set by the original code.
Rejected sensor IDs do not reach the added write.

The inserted code adds one byte to local peak stack use. The analyzed complete
initialization path already has a deeper call chain, so its overall foreground
maximum is unchanged. Instruction-model comparisons checked register/PSW and
live-stack preservation for both configuration values. The data-pin latch may
end differently after the added transaction; the transmitter releases that pin
after each byte. Physical timing and the exact sensor register map remain
hardware assumptions, despite the successful idle and resume tests on one unit.

## Optional polling profiles

The default candidate above keeps the original USB interval. Optional profiles
apply the same sleep fix and change only byte `0x1f36` from `0x0a` to `4`, `2`,
or `1`. The endpoint descriptor at `[0x1f30,0x1f37)` is
`07 05 81 03 04 00 0a`: interrupt IN endpoint `0x81`, four-byte maximum packet,
and a 10 ms polling request. GET_DESCRIPTOR serves this byte as part of the
configuration at `[0x1f15,0x1f37)`.

| `bInterval` | Complete image SHA-256 |
| --- | --- |
| 4 ms | `4169e8eec62913ff43bb8693b1144893575fbf9d93ecbbdc3e1b2716ce007028` |
| 2 ms | `187c6944854281080d8fc744a916182807dc08037d22bed894de39dff0310d0d` |
| 1 ms | `188b4d05d99f9a3274bdbbf51203e597eabe286b887338f6d4fd1d6e8fb4e561` |

These images differ from the original at 24 bytes and from the default
sleep-only image at one byte. They do not change the USB speed, clock setup,
timer reloads, or sensor transactions. The application was observed operating
at USB low speed. Sub-10-ms interrupt intervals are nonstandard at that speed;
[Linux's descriptor parser](https://github.com/torvalds/linux/blob/master/drivers/usb/core/config.c)
explicitly accepts them for overclocked devices.
[Linux xHCI](https://github.com/torvalds/linux/blob/master/drivers/usb/host/xhci-mem.c)
rounds the original 10 ms request down to 8 ms and represents 4/2/1 ms directly.
This is host scheduling behavior, not a measured device report rate.

There is no fixed 100 Hz software sampling gate in the traced USB path.
Endpoint completion at `0x2885` clears pending flag RAM `0x6c.1`, bypassing the
timer wait at `0x16bd`. After enqueue, the main loop calls the acquisition
wrapper at `0x16f6` to prepare another sample. The three-Timer0-interrupt
fallback applies while a report remains pending. Shortening that fallback can
discard accumulated changes when the endpoint is busy, so these profiles leave
it alone.

Faster polling can therefore increase acquisitions, but sustained fresh 1 kHz
reports remain unverified. Unchanged inputs need not produce reports. Sensor
frame rate, acquisition time, and interrupt load can limit throughput. Button
debounce requires 20 stable main-loop calls, and recovery delays also count
calls; their wall times can change with workload.

Installation accepts the exact original or a pinned generated image as its
starting state, while still requiring the original double backup. Only changed
pages are written: changing the polling interval alone changes page `0x1f00`.
The backup tool recognizes the exact V0114 descriptor variants, and exit from
a known image requires enumeration with that image's descriptors.

## Sensor resolution (CPI)

CPI is the number of movement counts per inch of surface travel seen by the
sensor. For a trackball this is ball-surface movement, not cursor travel on the
display. The documented PAW3205DB-TJ3T configuration register `0x06`, bits
`[2:0]`, offers these settings:

| Register value | Documented CPI |
| --- | --- |
| `0x00` | 600 |
| `0x01` | 800 |
| `0x02` | 1,000 |
| `0x03` | 1,200 |
| `0x04` | 1,600 |

The original initializer chooses `0x00` for sensor ID `0x30` and `0x01` for ID
`0x31`. These are programmed configuration values, not an identified physical
resolution ceiling. The exact installed sensor variant remains unconfirmed;
the table describes the cited sensor map, not measured CPI on this trackball.
The specification also does not establish how much additional independently
resolved detail its higher-CPI settings provide.

The builder accepts `stock` or any explicit CPI in the table above, independently
of the polling selection. `stock` leaves both original CPI instructions intact.
An explicit override makes both accepted-ID branches send `86 vv`, where `vv`
is the selected register value:

| Range | Original | CPI override |
| --- | --- | --- |
| `[0x2251,0x2253)` | `7f 01` — `MOV R7,#0x01` | `7f vv` — `MOV R7,#vv` |
| `[0x2260,0x2262)` | `e4 ff` — `CLR A; MOV R7,A` | `7f vv` — `MOV R7,#vv` |

Both branches still reach the existing configuration transmitter and sleep
trampoline. Changing the original writes avoids adding another sensor
transaction. The separate sleep-control write remains `85 b0`, and the shared
initializer reapplies both settings after reset, recovery, and resume. The CPI
override changes two bytes for 800 CPI, or three for the other explicit settings,
within page `0x2200`; it does not change the USB
descriptor, polling interval, clocks, or timers.

The transmitter clears A before using R7, so replacing `CLR A; MOV R7,A`
does not add an accumulator dependency. Transmitted data can change PSW's
overflow flag; the [CPI analysis](analysis/v0114-cpi.md#overflow-flag-difference)
explains why the returned flag has no identified consumer before being redefined.

All six CPI selections combine with each of the four polling intervals. The
generator and installer pin complete image hashes for all 24 profiles. The
[build command](../README.md#build-a-cpipolling-profile) names files using the
requested CPI and descriptor interval; a name does not assert measured CPI or
report cadence.

## How resolution and rates interact

The following quantities describe different parts of the motion path:

| Quantity | Meaning |
| --- | --- |
| Optical frame rate | How frequently the sensor images the surface. The cited map's adaptive frame-rate control uses 800, 1,200, or 2,400 frames/s during normal operation. |
| CPI | How many movement counts the sensor reports per inch of surface travel. This is a spatial scale, not a frequency. |
| Acquisition rate | How frequently firmware reads sensor deltas. The completion-driven scheduler and its fallback determine this; the descriptor alone does not. |
| USB polling rate | How frequently the host offers the interrupt endpoint a transfer opportunity. The device can respond with no data. |
| Input-report rate | How frequently reports actually reach the host. Unchanged input is suppressed; `evhz` measures host input-event cadence rather than optical frames or empty USB polls. |
| Display refresh rate | How frequently the display presents a new frame. It is independent of sensor CPI and USB polling. |

The owner measured approximately 250 Hz with `evhz` during movement using the
4 ms polling profile. That observation supports increased delivered input
cadence on the tested unit; it does not establish the optical frame rate or
effective CPI.

For an idealized example at 800 CPI and one inch/second of ball-surface travel,
the sensor produces 800 counts/second. Delivered at 125, 250, 500, and 1,000
reports/second, this averages 6.4, 3.2, 1.6, and 0.8 counts per report. Faster
reporting can distribute movement into smaller, more frequent updates. At very
slow movement, individual counts arrive less frequently than polling
opportunities; faster polling cannot subdivide a sensor count.

Increasing CPI produces more counts for the same ball rotation. Reducing
constant desktop pointer gain can keep cursor travel comparable while using
those smaller movement increments. With an adaptive acceleration profile,
gain also depends on movement speed, so a desktop speed setting is not a
direct CPI ratio. Hardware CPI, host gain, and acceleration must be considered
separately when comparing tracking precision.

Sensor deltas and USB X/Y reports are signed eight-bit quantities, and the
firmware saturates accumulated X/Y motion at -127/+127. Higher CPI increases
the counts accumulated between reads/reports and can reach those limits
sooner during fast motion. Faster acquisition/reporting reduces that pressure,
but does not establish that the sensor or host can sustain any requested rate.
The sleep fix retains adaptive frame-rate control and sleep1, so it also does
not promise a new optical frame on every 1 ms poll or eliminate sleep1 wake
latency.

## Flash layout and entry/exit

| Range | Role |
| --- | --- |
| `[0x0000,0x0b00)` | Loader code and descriptors |
| `[0x0b00,0x0c00)` | Persistent metadata/boot markers |
| `[0x0c00,0x4000)` | Application area, including padding |

Application entry receives an eight-byte SET_REPORT payload beginning `aa 55`.
Linux hidraw requires a leading report-ID byte `00`, so the host write is nine
bytes. Entry erases the metadata page and writes `55 55 01 01` at
`[0x0bea,0x0bee)` on the tested USB path, then takes the reset path.

Loader startup selects update mode when byte `0x0beb` is `0x55`. Otherwise it
jumps to application startup at `0x2f38`. There is no software application-validity
test or automatic corrupt-application fallback in this startup path.

Exit opcode 5 erases the metadata page and writes two copies of its old first
byte at `0x0b00/0x0b01`. It queues no ACK. Clearing the marker makes the loader
stop polling USB; return to application operation was observed on the tested
unit. The exact MCU reset-register semantics are not documented in the sources
available here.

## Loader wire protocol

The supported boot descriptor declares output report ID 1 and input report ID 5,
each with 63 data bytes: **64 bytes including the report ID**. Addresses in
commands are high byte first. Requests are zero-padded to 64 bytes.

| Operation | Request prefix | Reply |
| --- | --- | --- |
| Read | `01 03 count addr_hi addr_lo` | `05 01 04 count data...` |
| Erase application page | `01 01 01 relative_page` | `05 01 01` followed by zeros on success |
| Program | `01 02 count addr_hi addr_lo data...` | `05 01 02` followed by zeros on success |
| Exit | `01 05` | No reply |

Erase/program failure is `05 01 00` followed by zeros. Read tails beyond the
requested data are not required to be zero. Replies do not echo addresses.

Protocol constraints enforced by the host tools:

- Reads use 1–60 bytes and never cross the non-wrapping `0x4000` boundary.
  The firmware's own checks do not safely enforce these limits.
- Programming uses even addresses and even counts of 2–58 bytes. The loader
  programs pairs; an odd count would write one extra byte.
- Erase page numbers are relative to `0x0c00`. The installer's only page
  numbers are `0x13`, `0x16`, and `0x22`, corresponding to descriptor page
  `0x1f00`, hook page `0x2200`, and trampoline page `0x2e00`.
- Erase ACK verifies only the first two erased bytes. The installer checks all
  256 bytes before programming. Program ACK follows pair-by-pair comparison;
  the installer additionally verifies each page and two complete final images.
- Failed program verification can leave flash-control state and interrupts
  unclean. The host stops rather than retrying or automatically exiting.

Another protocol handled by the vendor updater assigns **write** semantics to
opcode 3. Exact device/report-layout validation is therefore essential; these
packets must not be sent to an arbitrary HID bootloader.

## Checksums

The traced V0114 reset, application, and interrupt paths contain no image-wide
software checksum or code-integrity scan. Reachable code-memory and XDATA readers
were classified, including generic pointer readers and computed dispatches.
The shared loader checks programmed bytes against their payload, but does not
validate a whole-image checksum at startup or exit. No software checksum field
needs updating for these changes.

The vendor updater erases the final page but omits programming `0x3ffe/0x3fff`,
leaving `ff ff`. Neither the application nor loader uses that word as a checksum.
The original and patched images retain it unchanged. Its hardware purpose is
unknown; it must not be replaced with a guessed checksum.

The vendor package uses Intel HEX record checksums and a HEX-text CRC32 naming
suffix. Those are outside the decoded raw image; the traced converter and
programming path do not enforce them. This project generates a raw image and
does not rebuild the vendor package.

The [SG8F7581 product page](https://www.sigmachip.com/en/keyboard-controller/sg8f7581/)
documents 16 KiB flash, 256-byte pages, and selected-region IAP. It does not
define hardware/ROM integrity checks or the final word. The software findings
are not a proof about undocumented silicon behavior.
