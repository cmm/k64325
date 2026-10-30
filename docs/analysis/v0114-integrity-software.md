# V0114 sleep2 candidate: software integrity requirements

2026-10-27. Static, offline findings. Firmware addresses below are both code
addresses and binary offsets; ranges are half-open. Updater addresses are PE
preferred-image virtual addresses. The analysis covers the original and default
sleep-only candidate identified below; the Windows updater was not executed.

## Result

**No software-side checksum adjustment is required for the sleep-only RAW
candidate by the analyzed application, shared loader, or traced updater
protocol.** No application image checksum, signature, length/footer validation,
or code-memory integrity scan was found in the bounded startup/main/interrupt
control-flow closure. The loader compares programmed bytes with the request
payload; it does not compare an image-wide checksum at startup or completion.
The candidate introduces no additional code-memory or XDATA read instruction.

This is stronger than searching for a literal `3ffe`: all reachable `MOVC`
sites and `MOVX` readers were classified, and the computed dispatches were
expanded. It is still **not an unconditional claim that ALL silicon/ROM
integrity checks have been identified**. MCU-controlled checks and the hardware
meaning of the final word remain for the separate MCU investigation.

The packaging checks must be kept separate:

- Intel HEX records contain checksums; the vendor records pass. These bytes
  are not part of the decoded RAW image.
- `5B37707F` in the mouse INI name is CRC32 of its HEX text. The inspected
  metadata, extraction, conversion and programming routines **do not enforce
  that CRC or the individual HEX record checksums**. The name suffix is not a
  device checksum field.
- The mouse updater sends no calculated image digest or signature. Its
  `0x3ffe` programming endpoint is a constant, not a checksum result or a
  decoded image-length field.

The sleep-only RAW candidate should not acquire an invented checksum word or
checksum-compensation change. Its `0x3ffe..0x3fff` remain `ff ff`. This finding
does not prescribe a flashing or marker-page procedure.

## Inputs and prior work

See the [sensor analysis](v0114-sensor-sleep.md) and
[firmware/protocol overview](../firmware.md) for the patch and loader context.

| Input | SHA256 |
| --- | --- |
| `backup-v0114/dump-1.bin`, 16,384 bytes | `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1` |
| Sleep-only candidate, 16,384 bytes | `7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074` |
| `firmware/expert-mouse-wired-v0116.bin`, 16,384 bytes | `be6985ff9b7d88878bd7b3ae9105ea5090490888b621cd48c0607ff1d4e694bd` |
| `Expert Mouse Wired V0116/KensingtonUpdate.exe` | `3c3ae52eff25a2859e67b7ba8408909f83990a42a940f8e860d0955e2a08766f` |

All hashes were rechecked. Loader `[0,0x0b00)` is byte-identical across source,
candidate and V0116; SHA256
`5522a686f9c1d1b75a962d36896a022ddda431f565f2cf8ed07f6616d377b6a5`.
Exactly 23 bytes differ between source and candidate:

```text
2262..2265  12 28 45 -> 02 2e 00
2e00..2e14  ff * 20 -> 12 28 45 c0 d0 7f 85 12 28 45
                       7f b0 12 28 45 d0 d0 02 22 65
```

Metadata `[0x0b00,0x0c00)` is unchanged, including post-entry
`0xbea..0xbed = 55 55 01 01`. This is not a normalized installation image.

## Control-flow bound

The traversal checked every loaded 8051 instruction's bytes against the image,
followed both conditional outcomes, direct callees and normal call
continuations, and stopped ordinary returns. It handled the C51 switch's
non-returning inline-table continuation separately. Inputs were independently
restarted V0114 and loader disassemblies, with additional radare2 decoding for
loader entries `0003`, `003c`, and `0046`.

| Roots | Unique instructions | Instruction bytes | Missing/overlapping/unexpanded targets |
| --- | ---: | ---: | --- |
| Application startup `2f38`, ISRs `3000/3200/3400/3600` | 4,124 | 7,644 | None |
| Reset `0000`, same ISRs, including loader and application | 5,555 | 10,278 | None |
| Candidate application, same roots | 4,132 | 7,664 | None |

This is a control-flow over-approximation, not a claim that every branch can
execute for one hardware/input state. The ISR destinations are also present as
literal vector jumps at `000b -> 3000`, `0033 -> 3400`, `0043 -> 3200`, and
`0073 -> 3600`.

All computed jumps in that closure have a bounded explanation:

| Jump | Origin and targets |
| --- | --- |
| Loader `02da: 73` | Request byte is restricted below `0x0c` at `02ca..02d3`; `DPTR=02db`, index multiplied by 3. Twelve LJMP entries `[02db,02ff)`. |
| Loader `06d6: 73` | Sole caller `082b`; `0827/0829` load RAM `1a/1b`. Startup table `[0036,003c)` initializes `19..1b = 00 2f 38`. Preceding MCU initialization `093b` does not replace that pointer. Destination `2f38`. |
| Application `0d79: 73` | Bounded twelve-entry standard-request table `[0d7a,0d9e)`, after request `<0x0c` check. |
| Application `150f: 73` | C51 switch helper `14f5` pops its caller's return PC into DPTR. Sole caller `0e83`; nineteen three-byte case records in `[0e86,0ebf)`, then `00 00 0f 6b` in `[0ebf,0ec3)`. All nineteen targets and default `0f6b` are followed. The table is data, not fallthrough instructions. |

Known descriptor, initializer, bit-mask and inline-switch data are not treated
as linear-sweep code. Unreferenced instruction sequences
`[226c,22ce)`, `[26ea,2715)`, `[29ae,29bb)`, and `2a01` were inspected as well;
they add no code-memory or XDATA reader. The unreferenced `26ea` helper writes
duplicated bytes at flash aliases `8b00/8b01`; it does not read/check the image.

Ordinary call/return semantics, intact RAM/stack, the identified interrupt entry
set, and standard 8051 instruction meanings are assumptions of this bound.
Unknown MCU vector/remapping/ROM behavior is not silently converted into a
software control-flow proof.

## Application code-memory reads

There are **sixteen MOVC instruction sites** in the application closure. These
are the complete groups, not examples selected by immediate address:

| MOVC sites | Source and use |
| --- | --- |
| `2f48,2f4c,2f5a,2f6a,2f85,2f93,2fa2,2fa6,2faa` | C51 RAM initialization. `2f7f: 90 29 3d` sets the table pointer. Actual short direct-IRAM records consume exactly `[293d,2950)`, including the zero terminator. No image scan. The generic bit-initialization path's PC-relative read `2f6a` indexes the eight mask bytes `[2f77,2f7f)`; it and the long/XDATA initializer forms are not selected by these table records. |
| `1446` | Generic pointer read `142f`; sole caller `117e` reads a string descriptor's length. String index is limited to 0 or 1; pointers come from initialized RAM `37..3c`, not from a host address. |
| `1473` | Generic indexed read `1448`; sole caller `2161` in endpoint copier `2111`. Source pointer is saved from USB descriptor state, with offset/copy count for the current packet. The same copier writes the result to the USB endpoint buffer at `216e`; no checksum state is accumulated or compared. |
| `14fb,1500,1505,1509,1512` | C51 switch-table lookup described above. Reads caller-inline case targets/values; compares a case value, not image contents. |

The application initializer bytes and effects are:

```text
293d 02 68 00 00                 IRAM 68..69 = 00 00
2941 01 6a 00                    IRAM 6a = 00
2944 01 6b 00                    IRAM 6b = 00
2947 06 37 ff 1f 6b ff 1f 6f     IRAM 37..3c = two code pointers
294f 00                          end
```

Descriptor pointer/length producers are `1119..11d4`. For V0114 they select:

| Object | Code source | Length |
| --- | --- | ---: |
| Device | `1f03` | `12` |
| Configuration | `1f15` | `22` |
| HID | `1f27` | `09` |
| Report | `1f37` | `34` |
| String 0 / languages | `1f6b` | `04` |
| String 1 / product | `1f6f` | `30` |

All lengths here are hexadecimal. USB transfer state is RAM `4b..4d`
(space/high/low) and `58..59` (remaining count). `11d8..1222` can reduce the
length to the host request; `1223..1289` and `0c4c..0cb2` copy at most eight
bytes at a time, advance the pointer, and reduce the remaining count. All four
calls to copier `2111` are at `0c69`, `0c9b`, `1240`, and `1272`.

The descriptor-error path `11d5` stalls and falls into transfer code using saved
state. This is not an image-validation branch. The pointer bound above assumes
normal descriptor-state invariants; it is not a formal proof against arbitrary
malformed USB sequences, memory corruption, or peripheral behavior. No
unresolved host-selected pointer or unexpanded dispatch was found in the
normal producer/consumer chain.

## Application XDATA / flash-alias reads

There are **twenty-one MOVX read sites** in that same closure:

| Sites | Address source / meaning |
| --- | --- |
| `0ce0,0ce5` | Fixed XDATA `0000/0001`, the OUT packet's `aa 55` entry prefix. |
| `0cfb,0d00,0d04,0d08,0d0c,0d10,0d14,0d18` | Fixed XDATA `0000..0007`, copied SETUP fields. |
| `0f74,0f79` | Fixed `8bea/8beb`, both compared with `55` before reset/wait. Boot-entry markers, not an image checksum. |
| `2079,208b` | Fixed `8bec`, persisted interface-mode byte. `207a` tests erased `ff`; `208c` compares it with detected RAM `3e`. Mismatch waits at `2093`. This operational consistency check does not depend on application bytes. |
| `1436,143f,1455,1466` | XDATA alternatives in the two generic-pointer helpers above. The descriptor producers use code tag `ff`; they do not supply a flash-alias/XDATA pointer. These generic alternatives are not an additional integrity reader. |
| `2334,2341,234e` | Fixed XDATA `ff23`, read/modify/write branches in peripheral setup `2327`. These are behind constant-zero tests in the supplied code; even the over-approximated addresses are not the flash alias. |

Thus the application's flash-alias reads are fixed metadata reads, not a loop
over executable bytes. No application data reader in the traced normal flow
uses `[2262,2265)`, `[2e00,2e14)`, or `[3ffe,4000)`. Executing the hook and
candidate trampoline is ordinary instruction fetch. The candidate adds calls
to the existing sensor transmitter and a PSW save/restore, with no MOVC/MOVX
reader; the original and candidate reader inventories are identical.

This bounds the absence claim: no image-integrity decision depends on either
changed range through the identified software read mechanisms. It does not
rule out silicon autonomously examining flash.

## Shared loader: startup, programming, completion

### Startup and every other loader code reader

Reset `0000: 02 05 4c` enters C51 startup. Its nine MOVC sites are
`055c,0560,056e,057e,0599,05a7,05b6,05ba,05be`. Actual initialization consumes
only `[0036,003c)`; the generic bit-mask reader `057e` would use
`[058b,0593)`, not application code. Startup jumps to `081c`.

At `081f: 90 8b eb; 0822: e0; 0823: 64 55; 0825: 60 07`, byte `0beb==55`
selects loader initialization; every other value takes the fixed indirect
application jump described above. There is no intervening application header,
version, length, signature, last-word or checksum validation.

The loader's only other MOVC is generic indexed reader `06cf` in `06a4`.
Its call sites are `04aa/04d2/04f4/0520` (program request payload), `077b`
(response buffer), and `07c4` (descriptor copying). Program payload and
response pointers are fixed IRAM objects with memory tag 0; descriptor calls
use code tag `ff` and fixed objects `[063f,06a4)`. This is not an unchecked
image-scanning subroutine hidden behind a generic helper.

The additional loader MOVX readers are fully accounted for:

- `018c`: opcode-3 requested flash read, copying `8000+address+i` into the reply.
  It can read the hook, cave and final word on request; it does not validate
  them or update a checksum accumulator.
- `01a9`: fixed `8b00`, the byte preserved by completion.
- `050c/0538`: the two programmed-byte comparisons described below.
- `080f/0814`: first two bytes of the selected erased page, compared with `ff`.
- `0822/0842`: fixed loader-selection marker `8beb`.
- `0298/029c/02a0/02a4/02a8/02ac/02b0/02b4`, `05f2/0609/0620`, and `08cd`:
  USB setup/endpoint-buffer reads, not image memory. `08cd` copies OUT data
  from XDATA `0008..0047`.
- `06b1/06c2`: XDATA alternatives in generic reader `06a4`, with the same
  bounded caller origins listed above.

### Per-command checks

- **Erase:** `00a5..00bd` accepts relative page `0..33` hex, adds `0c`, and
  calls `07e4`. All 52 application pages `[0c00,4000)` can be erased.
  `080f..081b` compares only each selected page's first two bytes with `ff`.
- **Program:** `00d2..00db` accepts counts `0..3a`; `00f1..0104` accepts start
  addresses `0c00 <= address < 3fff`. It does not check image length,
  signature, expected CRC, transfer completeness, or `address+count`.
  `0472..054b` writes in pairs. At `050c..0511` and `0538..053d` it XORs each
  read byte against that byte of the request payload and returns failure on a
  mismatch. There is no image-wide accumulation. Odd counts write one extra
  byte; normal host packets have even counts. Failure returns skip normal
  mode/EA cleanup, as noted in the earlier loader report.
- **Complete:** `019e..01a9` saves `MOVX[8b00]`; `01ac..01cd` erases metadata
  page `0b`; `01d2..01e6` programs two copies of the saved byte at `0b00/0b01`.
  The programming result is ignored. No app bytes or final word are read.
  `01eb..01ef` clears response state and returns without an ACK. Main then
  reads marker `8beb` at `0842`; a cleared marker leads to wait loop `0847`.
  Completion does not require any preceding erase or program command.

The intended checks are therefore erased-prefix comparison and per-request
programmed-byte comparison. There is no device protocol checksum field that
must be recalculated when the two candidate ranges change.

### Final page / `3ffe`

The `_AllSpeed` updater uses constant start `0c00` at `4094f1..4094f9`, 229
blocks of 58 (`409843` limit `e5`), then 28 bytes (`40985b..409930`). Thus:

```text
0c00 + 229*58 = 3fe2
last packet: [3fe2,3ffe), last pair [3ffc,3ffe)
erase last page: [3f00,4000)
omitted word: [3ffe,4000), left erased ff ff after successful page erase
```

There is no special last-page checksum branch in the loader pair loop. Its
start-address check even accepts `3ffe`; a two-byte request there would reach
the ordinary pair programmer. Opcode 5 does not fill a tail word. In source,
candidate and vendor V0116 that word already equals `ff ff`.

The full HEX represents all `4000` bytes and the updater allocates/fills a
`4000`-byte buffer (`404cf3..404d3a`). HEX conversion's size guard is also
`4000`, not `3ffe`. The omission is therefore **not a consequence of a smaller
decoded image or of a software footer/CRC validation policy in these paths**.
It is a fixed two-byte transmission omission compatible with a reserved-tail
convention. The binary does not say what hardware purpose motivated it;
calling it a hardware reservation still requires MCU documentation.

The separate non-64-byte-output protocol (`409ec0`) transmits source
`[1800,4000)` and is selected by HID report size, not a proven mouse-image
policy. The alternate keyboard resource has a different reset entry (`04fd`),
an INI suffix equal to its HEX CRC32 (`49D25954`), and `ff ff` at the end.
Neither fact proves anything about this mouse's tail or assigns that keyboard
image to the updater's other protocol.

## Windows updater HEX / CRC enforcement

For the cited `.text` instructions, file offset is VA minus `400c00`; for the
cited `.rdata` strings, file offset is VA minus `401400`.

### Resource loading and name suffix

`404b41` calls metadata parser `40b5b0` with type `INI`, ID `a3` (163).
`40b9f6..40ba26` uses ordinary `FindResourceW`/resource loading, not an
explicit-language lookup. The NAME branch `40bb99..40bbf5` searches for
`NAME:` (string VA `59cb4c`), copies its value to object field `+108`, and
checks for a nonempty value. Subsequent branches parse the six VID/PID/PVN
fields. Failure at this stage causes `404b48..404b63` to display
`This program has been damaged` (VA `59a358`); that message is not evidence
of a CRC comparison.

The name's use at `404b92..404c20` finds the first underscore (`404ba8` pushes
`5f`, `404bab` calls the wide-character search), takes the prefix, and sets the
window title. It does not convert the suffix to a CRC or compare it with file
contents. Other direct `+108` references are its constructor initialization
(`4047c8`) and destructor/reference-count cleanup (`404338`), and the parser
assignment/length check above. No name-derived digest is passed to HEX
conversion or transmission.

At `405b52` the resource type is `Hex` (string VA `59a81c`); `405b7e` selects
ID `9e` (158) and `405b83` calls resource-to-file helper `401f40`.
`401f87..401fab` obtains the resource bytes, `401fc7` writes them to the
temporary file, and `401fd4..401fdc` compares bytes-written with resource size.
There is no CRC calculation in that helper. `405b92..405be2` handles extraction
failure. The temporary name is built from `SGKBTemp`, not from the NAME CRC.

The mouse HEX is at EXE offset `1f0bc8`, length 39,435 excluding its resource
NUL. Its standard CRC32 is `5B37707F` including CRLF and excluding that NUL;
the decoded binary's CRC32 is `FE4D4E11`. The correspondence is verified, but
neither value is compared by those traced routines.

### HEX parser and enforced conditions

`405c6c..405c8d` fills the RAW buffer with `ff` and calls converter `402230`.
`405c99..405d12` branches on its Boolean result; a failure prevents the update
thread from starting. In `402230..402404`:

| Location | Action |
| --- | --- |
| `402246..40226e` | Opens the temporary file with mode `r` (VA `599f70`); checks EOF state. |
| `402280..4022b1` | Scans header using ASCII format `%1c%2x%4x%2x` at VA `599f74`. A scanned first character other than `':'` skips to the remainder-of-line consumer. The conversion-count return value is not checked. |
| `4022b7..4022dd` | For data type 0, skips zero-count records and accumulates nonzero data-byte counts; rejects cumulative count above `4000`. This is not a complete address/coverage check. |
| `4022f3..402327` | Scans each payload byte with `%2x` (VA `599fa8`) and stores it at base+record address+index. No checksum accumulation or byte comparison. |
| `402394..4023a6` | Consumes the remaining token with `%s\r\n` (VA `599f84`), including the record checksum text. The consumed checksum is not parsed as a checksum or validated. |
| `402331..402378` | Handles EOF type 1 and extended-address types 2/4; type 3 is skipped. Unknown types fail. |
| `4023e6..402404` | Recognized EOF returns success without requiring exactly `4000` data bytes, checking a final CRC/signature, or inspecting an image footer. |

The 512 vendor 32-byte data records and EOF record all pass Intel HEX checksum
validation in an independent standard-library reconstruction, and cover
`0000..3fff` once. That valid packaging should not be mistaken for enforcement
by the converter. Re-encoding an image into valid Intel HEX requires new record
checksums as a format requirement; **the present RAW candidate has none**.
Maintaining the INI CRC naming convention in a hypothetical new package is
separate from any checksum required by this device or this converter.

After conversion, the load path extracts another support resource and starts
thread `406010 -> 406020` at `405e7e..405e83`. Orchestration selects `4094b0` or
`409ec0` by report size at `4064b6..4064ce`. The mouse programming routine
reads bytes from object field `+cc` and sends addresses/data, then command 5;
it does not calculate an image CRC or append a generated checksum to those
packets. See the [loader protocol](../firmware.md#loader-wire-protocol) for ACK
and completion behavior.

This is not a recursive audit of the entire PE. Orchestration first calls
driver-preparation helper `4070c0` at `40607f`; it also calls helper `402520`
at `40629f` (which obtains `kernel32.dll` version information via `402410`).
The driver helper's inspected strings/calls cover SGKBLIBUSB extraction,
service/filter installation and Windows-device discovery. Their complete
OS/framework call closures were not followed. No firmware-buffer `+cc` or
NAME-CRC input was found there, but an assertion about every possible
updater-wide damage check would require closing those calls as well. This
limit does not introduce a checksum field into the RAW image or mouse protocol.

The outer PE optional-header CheckSum is zero and its certificate-table
directory is empty. These are packaging facts about the unmodified executable,
not firmware checksum fields.

## Verification and remaining limits

Disposable offline checks verified input hashes,
exact candidate differences, loader identity, table decoding, descriptor bytes,
unchanged candidate read-instruction sets, complete Intel HEX reconstruction
and known CRC32 correspondence. They also checked 2,158 cited PE disassembly
instruction byte strings against the identified EXE. The RAW image was not
adjusted. No checksum brute force was used.

**Confidence:** high for the supplied loader's lack of an image-wide software
checksum and for the absence of application integrity scanning in the stated
closure. The PE record-checksum omission follows directly from the conversion
instructions; the absent NAME-CRC gate is bounded to the traced metadata,
extraction, conversion and programming path. The Windows program was not run;
this is not a native execution test of its CRT/parser.

**Still unknown:**

1. Whether SG8F7581 hardware/ROM, configuration words or CPU/peripheral
   extensions impose integrity checks not represented by the enumerated CPU
   reads; specifically the hardware meaning/protection of `3ffe/3fff` and the
   flash-control mapping through SFRs `fe/ff`. The
   [manufacturer product page](https://www.sigmachip.com/en/keyboard-controller/sg8f7581/)
   does not specify those semantics.
2. Whether the real interrupt/vector/ROM and RAM/stack rules satisfy the
   software-closure assumptions. Corruption-induced execution, unknown hardware
   entries, and adversarial USB-state sequences are outside the absence proof.
3. Which INI/HEX resource language Windows selects on a particular host. Both
   load calls use resource fallback. No V0114 vendor package or packaging CRC
   has been supplied; none is needed to describe this RAW candidate.
4. Physical flash/readback/reset behavior and installation/recovery on the
   device. The software's byte comparisons are not a live programming test.
5. Any additional updater-wide checks outside the enumerated metadata,
   extraction, conversion and programming routines; the unexhausted helper
   entries are `4070c0` and `402520`, called from `40607f` and `40629f`.

Accordingly: **there is no identified checksum repair missing from this RAW
candidate; claiming that every possible device integrity requirement is settled
still requires the hardware-side result.**
