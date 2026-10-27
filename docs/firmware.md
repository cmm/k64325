# Firmware and protocol notes

These notes describe the hash-pinned V0114 image supported by the tools. USB
release `0114` is an advertised version field, not proof of identical code.
Addresses below are byte offsets in the 16 KiB flash image; range ends are
exclusive.

## Identification and provenance

| Object | SHA-256 |
| --- | --- |
| Original, after bootloader entry | `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1` |
| Patched image | `7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074` |
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
  numbers are `0x16` and `0x22`, corresponding to `0x2200` and `0x2e00`.
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
needs updating for these two changed ranges.

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
