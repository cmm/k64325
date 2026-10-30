# V0114 optical-sensor sleep: offline findings

2026-10-27. Addresses are code addresses and file offsets. Ranges written
`[start,end)` exclude `end`. This analysis describes the original firmware.
The resulting sleep2 patch and observed device behavior are summarized in the
[firmware notes](../firmware.md#sensor-change) and
[compatibility section](../../README.md#compatibility-and-risk).

## Result

**Established from instruction bytes:** the common initializer at `0x21fd`
transmits `86 80`, reads using address byte `00`, and transmits `86 00` or
`86 01`. All 13 direct calls to its sensor transmitter were traced. There are
no write address bytes `85`, `89`, or `8a..8c` for the cited map's
`Operation_Mode`, write protection, or sleep timing registers. The only other
write transaction is `86 08` in the USB-event-driven suspend handler. No
separate approximately 20-second firmware command to power down the sensor
was found in those transactions.

**Conditional sensor interpretation:** if this board uses the saved
PAW3205DB-TJ3T register map and reset defaults, `86 80` resets it and `86 00/01`
sets its normal configuration/CPI. Firmware leaves automatic sleep1 and sleep2
enabled: `Operation_Mode=0xb8`, `Enter_Time=0x10`. Those defaults give
256 ms to sleep1 and another 20,480 ms to sleep2. This matches the reported
timing, but neither the actual sensor suffix, live ID, nor live registers were
measured. Acceptance of IDs `0x30` and `0x31` by firmware does not identify the
installed part.

**Sleep2 change:** add the transaction `85 b0` after each successful sensor
reset/configuration. On that register map this clears only automatic sleep2
enable, retaining sleep1. It requires inserted code; no existing `0x05` write
has an operand that can simply be changed. A shared hook is identified below.
The register interpretation remains conditional on the sensor map.

## Inputs and sources

| Input | SHA256 |
| --- | --- |
| `backup-v0114/dump-1.bin` | `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1` |
| `backup-v0114/dump-2.bin` | `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1` |
| `firmware/expert-mouse-wired-v0116.bin` | `be6985ff9b7d88878bd7b3ae9105ea5090490888b621cd48c0607ff1d4e694bd` |
| Saved `paw3205-spec.pdf` | `eeac6ef47b164886ecc9393f28dc9efb33a1719c3db0ba993882b04f3fef23ae` |

Both dumps are 16,384 bytes and identical. `[0,0xb00)` equals V0116 exactly.
The dumped metadata `[0xb00,0xc00)` is all `ff` except
`0xbea..0xbed = 55 55 01 01`. This is the **entry-modified** metadata supplied
for analysis, not a measurement of the marker page after the user's guarded
exit. The user reports the mouse returned as `bcdDevice 0114`.

Sensor source: PAW3205DB-TJ3T specification V0.2, November 2011,
<https://github.com/Li-Dongze/paw3205_mouse/blob/main/PAW3205DB-TJ3T_SPEC_V0.2.pdf>.
Board text `MS-6070U-3205_Sensor` supports this family hypothesis;
no package marking is available.

## V0114 sensor transactions

TX is `0x2845`, RX `0x27ff`. TX consumes R7 MSB first and produces eight clock
cycles; RX returns the byte in R7. The PAW3205 protocol uses two bytes per
transaction, with address bit 7 set for writes (datasheet pp. 16–18).

An important V0114/V0116 hardware difference is visible here:

| Function | V0114 bytes | V0116 bytes |
| --- | --- | --- |
| RX data sample | `2817 a2 81` — P0.1 | `2813 a2 82` — P0.2 |
| TX data high/low | `2852 d2 81`, `2856 c2 81` | `284e d2 82`, `2852 c2 82` |
| TX port-control mask | `2847 53 91 f5` | `2843 53 91 f3` |
| TX port-control finish | `2863 43 91 02` | `285f 43 91 04` |

Both clock P0.3. Thus V0116 is not merely the same image with relocated code.
Do not substitute its entire initializer/transmitter or whole firmware image.

### Every direct TX call

Every call below has bytes `12 28 45`. Raw whole-image matches and decoded call
targets agree; no additional direct TX calls were found.

| Call address | R7 source/value | Transaction |
| --- | --- | --- |
| `0fd5` | `0fd3 e4`, `0fd4 ff` → `00` | Suspend-side ID read address |
| `1015` | `1013 7f 86` | Write address `06` |
| `101a` | `1018 7f 08` | Data `08`: forced power-down on the candidate map |
| `1575` | `1573 e4`, `1574 ff` → `00` | Poll-side ID read address |
| `15c5` | `15c3 7f 02` | Motion-status read address |
| `15df` | `15dd 7f 03` | Delta-X read address |
| `15f9` | `15f7 7f 04` | Delta-Y read address |
| `21ff` | `21fd 7f 86` | Write address `06` |
| `2204` | `2202 7f 80` | Data `80`: full-chip reset on the candidate map |
| `2213` | `2211 e4`, `2212 ff` → `00` | Initialization ID read address |
| `224e` | `224c 7f 86` | Write address `06`, ID `31` branch |
| `225d` | `225b 7f 86` | Write address `06`, ID `30` branch |
| `2262` | `2251 7f 01` or `2260 e4`, `2261 ff` | Configuration data `01` or `00` |

Every direct RX call has bytes `12 27 ff`, at `0fdf`, `157f`, `15cf`, `15e9`,
`1603`, `221d`. These complete reads of registers `00`, `00`, `02`, `03`, `04`,
`00`, respectively. No read of the current sleep settings or operation state
was found in this interface.

Outside TX/RX, the other individual P0.3 operations in the decoded application
are the low/high recovery pairs at `0ff0/0ff9`, `159a/15b0`, and `222d/2239`.
They do not send a configuration byte. Datasheet §6.2 distinguishes serial-port
resynchronization from chip reset: its clock/watchdog sequence does **not**
reset registers. The explicit register reset transaction above is unique among
the traced sensor writes.

### Common reset/configuration initializer

Relevant exact instructions:

```text
21fd 7f86      mov r7,#86
21ff 122845    lcall 2845
2202 7f80      mov r7,#80
2204 122845    lcall 2845
2207 75b736    mov B7,#36
220a 7f0a      mov r7,#0a
220c 7e00      mov r6,#00
220e 122867    lcall 2867       ; delay loop, argument 000a
2211 e4        clr a
2212 ff        mov r7,a
2213 122845    lcall 2845       ; register 00
2216 7f01      mov r7,#01
2218 7e00      mov r6,#00
221a 122929    lcall 2929
221d 1227ff    lcall 27ff
2220 785b      mov r0,#5b
2222 ef        mov a,r7
2223 f6        mov @r0,a
2224 6430      xrl a,#30
2226 601b      jz 2243
2228 e6        mov a,@r0
2229 6431      xrl a,#31
222b 6016      jz 2243
```

The rejected-ID path `[0x222d,0x2243)` toggles the clock around a delay, sets
retry RAM `0x5e:0x5f` to `00 b0`, and returns at `2242 22`. That RAM value
`0xb0` is **not** an existing sensor-mode write.

The accepted-ID continuation is:

```text
2243 121cec    lcall 1cec       ; other input servicing, not a sensor reset
2246 785b      mov r0,#5b
2248 e6        mov a,@r0
2249 b43109    cjne a,#31,2255
224c 7f86      mov r7,#86
224e 122845    lcall 2845
2251 7f01      mov r7,#01
2253 800d      sjmp 2262
2255 785b      mov r0,#5b
2257 e6        mov a,@r0
2258 b4300a    cjne a,#30,2265
225b 7f86      mov r7,#86
225d 122845    lcall 2845
2260 e4        clr a
2261 ff        mov r7,a
2262 122845    lcall 2845       ; shared final configuration-data transmit
2265 786c      mov r0,#6c
2267 e6        mov a,@r0
2268 4440      orl a,#40        ; mark sensor ready
226a f6        mov @r0,a
226b 22        ret
```

### Initialization, fault recovery, and resume callers

Both direct initializer calls are `12 21 fd`:

- `25c8`, in polling routine `0x2592`, called from main at
  `16e6 12 25 92` and `16f6 12 25 92`.
- `25fc`, in polling routine `0x25cc`, called from main at
  `1783 12 25 cc`.

Both pollers test RAM `0x6c.6`: if set, they tail-jump to acquisition at
`0x1570` (`25a4 02 15 70`, `25d8 02 15 70`). If clear, they count down
`0x5e:0x5f` and call the shared initializer when zero. The `0x2592` path also
requires RAM `0x42.4`. Polling with an invalid ID clears ready using
`15bc 54 bf`, stores a retry countdown `00 a0`, and returns.

After USB-related MCU wake, `[0x10fe,0x1116)` sets retry countdown `00 40`,
clears ready (`110d 54 bf`, `110f f6`), and updates other event bits. Polling
then returns through the same initializer. A change on its successful common
configuration path is therefore reapplied on initial setup, fault retry, and
post-suspend reinitialization. Failed-ID returns must remain separate.

## USB suspend and boot-entry boundaries

The main loop still calls `16ac 12 0f 6f`. Before testing the suspend flag,
`0x0f6f..0x0f81` checks XDATA `8bea/8beb` for `55 55`; on a match it executes
`0f7d 75 b7 36` and waits at `0f80 80 fe` for reset. This is part of
application-mediated loader entry and must not be bypassed.

At `0f82 78 6c`, the handler tests RAM `0x6c.0`; without it the suspend path
returns. Producer `0x2983` contains:

```text
2983 43cf08    orl CF,#08
2986 53cef7    anl CE,#f7
2989 786c      mov r0,#6c
298b e6        mov a,@r0
298c 4401      orl a,#01
298e f6        mov @r0,a
298f 43ce08    orl CE,#08
2992 22        ret
```

USB ISR tests the event at `3247 30 e3 03` and calls this producer at
`324a 12 29 83`. The suspend handler sends `86 08` at `0x1013..0x101c` and
executes MCU power-down at `1060 75 87 02`. These are distinct from automatic
sleep controlled by sensor register `0x05`. Their instructions and the boot
marker path must remain intact. The sensor's automatic sleep timing and host
USB suspend are separate mechanisms.

The shared loader tests persistent flash `0x0beb == 0x55`. Preserving loader
bytes does not guarantee recovery from an application that cannot enumerate
or process boot entry. With no programmer, installation remains consequential
even if the intended handler is left in place.

## Comparison with V0116

Whole-image same-offset comparison finds 2,920 differing bytes in 244 runs.
That comparison alone is misleading for instructions containing addresses.
The following regions were checked byte-for-byte after replacing only the
identified LCALL targets with their V0114 addresses:

| V0114 region | V0116 region | Result |
| --- | --- | --- |
| `21fd..226b` | `21f9..2267` | Identical apart from TX/RX/delay target relocation |
| `2592..25ff` | `258e..25fb` | Identical apart from initializer/delay target relocation |
| `1570..1610` | same addresses | Identical apart from TX/RX/delay target relocation |

Confirmed target mappings are TX `2841→2845`, RX `27fb→27ff`, delay
`2863→2867`, short delay `2925→2929`, initializer `21f9→21fd`. This is a
verified mapping of these symbols, not a universal `+4` rule.

Thus V0116 makes the same reset/configuration transactions and also does not
program sleep registers in the traced interface. It offers no existing
sleep-setting write to copy. In addition to the P0.1/P0.2 difference above,
the suspend handler has nonrelocation differences:

- `1028` and `1036`: V0114 `90 ff 19`; V0116 `90 ff 1c`.
- `10b6`: V0114 `e5 90`; V0116 `e5 c0`.

The suspend branch structure and sensor data values agree; USB/peripheral
register accesses are not identical.

## Datasheet constraints on a candidate

PAW3205DB-TJ3T pp. 4, 7–10 and §§6.2, 6.4–6.5:

- `0x05` reset default is `b8`. Bits `[6:5]` must stay `01`; bit 7 controls
  LED shutter. Bit 4 enables sleep, bit 3 enables automatic sleep2. Bits 2–0
  are force-sleep2, force-sleep1, and wake commands; at most one may be asserted
  in a write and they self-clear.
- **`b0`** clears only bit 3 relative to the documented default: normal/sleep1
  operation remains, without automatic sleep2. It does not disable all sleep.
- **`a0`** clears bits 4 and 3. It disables sleep and also AFC; this is a wider
  behavior change requiring a separate decision.
- To force normal mode while already asleep, the datasheet requires clearing
  sleep-enable bits and then asserting Wakeup. A post-reset application point
  avoids intentionally modifying an already sleeping sensor, but actual timing
  and state at that point have not been measured. A mid-run write must not be
  assumed to wake an already sleeping sensor.
- Write protection `0x09` applies only to `0x0a..0x7f`; changing `0x05` does not
  need an unlock. Changing timing registers would require `0x09=0x5a`.
- `0x0b=10` means sleep1 entry `(1+1)*128 = 256 ms` and sleep2 entry
  `(0+1)*20,480 = 20,480 ms`. Defaults `0x0a=70` and `0x0c=70` give sleep
  sampling intervals of 32 ms and 512 ms. No traced firmware write changes them.
- `0x06` bit 7 is full-chip reset and bit 3 is forced power-down. Its low bits
  select CPI. Replacing its normal `00/01` or suspend `08` values with a sleep
  mode value would target the wrong register.
- Serial resynchronization leaves registers intact. Forced power-down still
  permits serial reads/writes; after leaving it the datasheet specifies about
  3 ms before further operations for an accurate report. Existing reset,
  identification, configuration, and recovery sequencing should be preserved.
- The datasheet recommends verifying a write by reading the same address.
  Firmware currently does not verify a sleep-setting write because none exists.

## Insertion point and preservation constraints

At `2262 12 28 45`, both recognized-ID branches have already sent address
`86` and have R7 equal to the required data `00/01`. A three-byte long-jump
trampoline can execute that displaced transmit, append a separate `85 b0`
transaction, then resume at `0x2265` before the ready flag is set. It must not
replace or omit the pending `00/01` byte. This confines the change to successful
configuration and reuses the common path after every register reset/retry.

There are 1,334 consecutive `ff` bytes in `[0x2a02,0x2f38)`, between application
routines and startup. For example `0x2e00` is inside that block. No direct
branch/call target or fixed pointer operand into the block was found in the
independently decoded application/startup/ISR regions. This is plausible
padding, not a proof against every computed code/data reference or a checked
flash allocation. It avoids the shared loader, marker page, interrupt entries,
and reserved final-word question. A small helper fits without relocating code.

Preservation constraints established from TX:

- It uses A, R6, R7 and arithmetic flags; on return A and R7 are zero and R6 is
  eight. The final CJNE clears carry. Other arithmetic flags can differ with the
  data: adding a final `b0` transmit is not proof of identical PSW state to the
  original ID-`31` data transmit. Preserve live status or prove it dead.
- TX itself does not modify R0, DPTR or B. Additional helper/delay calls must
  not be assumed to share this clobber set. In particular `0x1cec` services
  other inputs and RAM and is not a generic sensor delay.
- A long-jump trampoline returning by long jump can keep the existing call
  depth for each TX; a call-wrapper adds another two-byte return address while
  TX runs. Every preservation push adds stack usage. Startup initializes SP
  with `2f3e 75 81 71`; stack use must account for interrupt nesting as well.
- Keep the recognized-ID/error branches, ready flag, USB suspend/wake flags,
  boot-entry marker checks, and original register-`06` transactions intact.

A read-modify-write clearing bit 3 could preserve a verified nondefault
`Operation_Mode`, at the cost of a read and its timing/preservation needs.
Neither it nor a constant `b0` write makes an unidentified sensor compatible
with this datasheet. The constant is the smaller candidate only under the
post-reset/default-register assumption.

## Verification and limits

The analysis used radare2 6.2.4 8051 decoding. Independently restarted decoding
over application code, sensor functions, main loop, startup and ISR entries
covered 4,357 unique instruction addresses, each checked against raw bytes.
V0116 comparison ranges covered another 457 byte-checked instruction addresses.
Checks compared hashes/metadata, raw and decoded sensor call lists,
relocation-only regions, and padding references.

All restarted sensor/main/suspend instructions agree with the original linear
decode. Startup's inline data at `0x2f77..0x2f7e` misaligns a linear sweep;
restarting at the actual `0x2f7f` target corrects that stream and removes its
spurious branch into padding. Initializer data `0x293d..0x294f` is not treated
as executable code. These are disassembly/byte checks, not sensor emulation
or a hardware sleep/resume test. Input image hashes were rechecked.

The implemented trampoline preserves PSW and disables only automatic sleep2.
See the [patch description](../firmware.md#sensor-change) for its instruction
sequence and stack result, and the [integrity analysis](v0114-integrity-software.md)
for image-checksum constraints. The actual sensor map, physical serial timing,
and recovery on other units remain hardware questions.
