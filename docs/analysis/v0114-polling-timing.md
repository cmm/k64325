# V0114 internal report timing and rate experiments

2026-10-27. Offline analysis of the original V0114 readback, SHA-256
`c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1`.
Addresses below are file offsets and code addresses.

## Result

**The USB path does not impose a fixed 100 Hz software sample period.** Its
normal changed-report cycle follows endpoint completion: completion clears RAM
`0x6c` bit 1, which bypasses the timer wait even when the timer count is zero.
After queuing a report, the main loop immediately acquires another sensor
sample for the next report. Faster host transactions can therefore cause
faster fresh acquisitions with the existing internal scheduler.

The descriptor-only candidates at `0x1f36` (`0a -> 04/02/01`) use this existing
dataflow. Throughput depends on sensor acquisition, report processing,
interrupt load, sensor frame rate, and sleep state. Descriptor/bus/host compatibility is
covered in the [USB analysis](v0114-polling-usb.md).

Keep the timer fallback unchanged: it permits processing while an earlier
report is pending, and the report routine can discard accumulated data when
the endpoint is busy.

## Timer and main-loop gates

Exact V0114 instructions, checked against the original binary:

```text
000b 023000    ljmp 3000
28d4 758931    mov 89,#31
28da 758a78    mov 8a,#78           ; TL0
28dd 758cec    mov 8c,#ec           ; TH0
28e0 758e05    mov 8e,#05
28e3 c28d      clr 88.5
28e5 d2ad      setb a8.5
28e7 d28c      setb 88.4

3009 758a78    mov 8a,#78
300c 758cec    mov 8c,#ec
3010 786f      mov r0,#6f
3013 94fa      subb a,#fa          ; preceding CLR C: unsigned count < 250
3017 9400      subb a,#00          ; high byte at 6e
3019 5007      jnc 3022
301b..3021    increment 6e:6f
3022 c28d      clr 88.5
302a 32        reti
```

The reload is `0xec78`, nominally **5,000 timer-input counts**, with software
reload/interrupt latency in addition. RAM `6e:6f` is big-endian, increments
once per ISR while below 250, and then stops incrementing. The ISR's ordinary
increment path executes 24 instructions; that is not a clock-cycle or time
measurement. Startup calls setup through `295c 12 28 d4`; suspend disables the
interrupt at `1059`, and wake reenables it at `10f2`.

The timer-input frequency is unknown. Converting counts to milliseconds needs
the divider/oscillator mapping for `8e=05` and the clock setup at `279f..27b2`.

USB main loop (`RAM 3e == 1`, branch at `1689..168f`):

1. `16a9..16bb` services suspend, other events, buttons, and alternating-pass
   wheel sampling before the report wait.
2. `16bd..16ce`: if `6c.1` is set and `6e:6f <= 2`, skip acquisition/report
   processing to `16fe`. `SETB C` at `16c4` makes the comparison **strictly
   greater than 2**, not greater than or equal to 2.
3. `16d0..16d9`: if no report is pending, clear the software wait counter
   `70:71`. There is no minimum timer count on this route.
4. `16da..16e6`: if `6e:6f > 2`, call acquisition/recovery wrapper `2592`
   before report processing.
5. `16e9 12 17 8f`: process accumulated inputs and possibly queue a report.
6. `16ec..16f6`: call `2592` again if `6c.1` is now set, or if the sensor-ready
   bit `6c.6` is clear. Thus an ordinary successful enqueue causes immediate
   acquisition; a pending-report timeout can execute **two** acquisitions in
   one processed pass, before and after the report attempt.

`2592..2597` clears `6e:6f` **before** checking configuration/readiness. If
configured (`42.4`) and ready (`6c.6`), `25a4` tail-jumps to acquisition at
`1570`. Otherwise it services the retry countdown. The alternate-mode wrapper
`25cc` also clears the timer count, but is called from the `3e == 0` branch,
not the USB report loop. EP0 entry also clears the count at `0c0d..0c11`.
These resets and asynchronous ISR service mean the gate is not an exact
periodic clock.

## Endpoint completion, enqueue, and freshness

The endpoint-event connection is visible within V0114, without assigning a
manufacturer register name:

```text
27c7 4410      orl a,#10           ; configured flag in RAM 42
27ca 43ce02    orl ce,#02          ; enable event used below
27d0 54fd      anl a,#fd           ; clear initial pending flag in RAM 6c

324d e5cf      mov a,cf
324f 30e103    jnb acc.1,3255
3252 122885    lcall 2885
2885 43cf02    orl cf,#02
288b 786c      mov r0,#6c
288e 54fd      anl a,#fd           ; clear pending
2891 54f7      anl a,#f7           ; clear post-wake bit 3
2895..289f    clear 70:71, 6d, 57; leave 6e:6f intact
```

This is the completion/release path used by the software scheduler. Exact
vendor meanings of `CF`, `CE`, and `C3` have not been checked against a register
manual; the flag/dataflow above is byte-established.

`178f..1819` rotates/scales accumulated motion. `181b..182f` skips sending if
buttons are unchanged and X, Y, and wheel deltas are all zero. For changed data:

- `1833` remembers the current buttons in RAM `61`.
- `1835..183d` requires configuration (`42.4`) and `C3.7` set before enqueue.
- `1840..1858` copies buttons/X/Y/(optional wheel) to XDATA `0008..000b`.
- `185b..1873` clears `FC`, writes length 4 or 3 to XDATA `ff21`, and writes
  `C3=44` or `43`.
- `1874..1879` sets pending bit `6c.1`.
- `187a..1881` clears X/Y/wheel accumulators. **The not-configured and
  `C3.7 == 0` paths also reach this clearing code after remembering buttons.**

Consequences:

- Completion-driven reporting need not wait for three timer ticks. A shorter
  host interval can produce a new acquisition after each enqueue even if no
  timer ISR occurred between reports.
- Reports contain buffered deltas, not a synchronous sensor read at each host
  token. The completion-driven sequence is enqueue -> acquire -> wait for completion ->
  enqueue acquired data. The timeout path can acquire before enqueue instead.
- There is one report buffer and a software accumulator, not a queue of all
   sensor samples. Unchanged input is suppressed.
- Forcing the timeout sooner can make changed data reach the busy-endpoint
   clearing path and lose motion.

## Acquisition and other consumers

`1570..1685` reads ID `00`, status `02`, delta X `03`, delta Y `04`. Valid IDs
are `30/31`; each successful call performs four address TX bytes and four RX
bytes, **64 sensor-clock cycles**, four short-delay calls (`2929`, argument 1),
and four wheel-service calls (`1cec`). It adds signed deltas to RAM `3f/40`,
saturating each at -127/+127. There is no independent 100 Hz delay here, and
the motion-status value is not used to skip the X/Y reads.

| Consumer | Timing relationship / effect of faster reporting |
| --- | --- |
| Sensor recovery | Invalid ID at `159a..15bf` performs clock recovery, clears ready, sets `5e:5f=00a0`. Initialization rejection sets `00b0`; startup/wake sets `0040`. `2592/25cc` decrement once per poller call, with two argument-12 busy waits and wheel service. These are call counts, not Timer0 ticks; altered call frequency can change recovery wall time. |
| Sleep change | Successful initialization still reaches `2262 -> 2e00..2e13 -> 2265`. Added sleep-register TX occurs on initialization/reinitialization, not every sample. Descriptor/timer operand sites do not overlap it. |
| Buttons | `2412` runs once per USB main pass, before the pending wait. `2433..244e` requires 20 stable calls after a raw change before updating RAM `60`. No millisecond debounce clock is present. More acquisition/report work changes main-pass cost, so debounce time cannot be assumed unchanged or proportional to USB rate. |
| Wheel | `1cec` samples quadrature, updates RAM `65/66`, accumulates wheel in `5d`, limited to +/-7. Called on alternating USB main passes and four times within valid acquisition, plus recovery/initialization paths. Its cadence changes with workload; it is not clocked by Timer0. |
| Pending/error recovery | `16fe..1735` increments `70:71` in main-loop passes while pending (unless `6c.3` is set), compares the prior value with 65,000 using helper `14df`, then considers error counter `57 >= 5` for USB reinitialization. This is not elapsed time. |
| Suspend | `6d` counts suspend-handler events; it is not incremented by Timer0. Suspend/wake and loader-entry handlers need no rate experiment edits. |

The literal `100` at `28bc 75 09 64`, and rate table `13f0..142e`, belong to
the alternate serial/PS/2 branch's `09/3d` handling; they do not drive the USB
completion/timer path. Do not patch that literal to change USB reporting.

Conditional on the [PAW3205DB-TJ3T V0.2 map](https://github.com/Li-Dongze/paw3205_mouse/blob/main/PAW3205DB-TJ3T_SPEC_V0.2.pdf):
status reads freeze X/Y before they are read; normal AFC uses
2400/1200/800 frames/s. The sleep candidate retains AFC and sleep1,
whose documented default sampling interval is 32 ms. Optical sampling and
sleep1 wake latency therefore remain separate from the acquisition rate.

## Bounded operand candidates, if measurements require them

| Instruction / operand offsets | Possible edit | What it actually changes |
| --- | --- | --- |
| Descriptor byte `1f36 = 0a` | `04/02/01` | Host interval; internal completion-driven sampling can follow it. No clock/speed migration is implied. |
| `16c8 94 02`, operand **`16c9`**, and `16de 94 02`, operand **`16df`** | Both `02 -> 01` or both `02 -> 00` | Pending wait/pre-report polling threshold becomes >=2 or >=1 ISR increments. Does not accelerate the completion-cleared route. Keep both comparisons consistent. |
| `28da 75 8a 78`, operand **`28dc`**; `28dd 75 8c ec`, operand **`28df`** | TL/TH reload bytes | Initial timer period. |
| `3009 75 8a 78`, operand **`300b`**; `300c 75 8c ec`, operand **`300e`** | Same TL/TH reload bytes | Repeating timer period and ISR load; update initialization too for consistent startup. |

Clock-independent examples: 5,000 counts uses TH:TL `ec:78`; 2,500 `f6:3c`;
1,250 `fb:1e`; 1,000 `fc:18`; 500 `fe:0c`. These give nominal timer-frequency
ratios 1, 2, 4, 5, 10 before latency.
With threshold 2, a timeout still needs three increments. Lowering the
threshold alone offers at most a three-to-one reduction in that count, not a
ten-to-one increase in normal host-driven throughput.

The timer's only identified application counter is `6e:6f`; buttons, wheel,
retry delays, and the pending/error loop do not share its ticks. Faster timer
interrupts nevertheless consume CPU and can interrupt GPIO sensor transactions.
Changes to `8e`, oscillator/divider writes, or sensor delay operands require
their hardware timing definitions. Measure endpoint readiness, acquisition
execution time, and optical/sleep behavior to identify the limiting stage.

## Checks

- All 4,357 unique instructions in the targeted disassembly matched the original
  raw bytes. Control-flow conclusions use identified routines, not descriptor
  data that the linear decoder also presents as instructions.
- Disposable instruction-model probes checked 131,072
   subtraction/carry cases for the comparison extension. Thirty-six actual-byte
  main-gate cases confirm completion-cleared bypass and the two-acquisition
  timeout route. Timer tests confirm saturation; report-tail tests confirm
  ready/busy writes and clearing; button tests confirm 20-call debounce. Two
  actual TX/RX acquisition executions confirm address sequence and new signed
  delta accumulation for both accepted IDs.
- Scheduler probes stub sensor acquisition/report processing; those paths are
  exercised separately. Acquisition probes stub wheel service and short delays.
  Hardware event timing, host transactions, optical processing, and concurrent
  interrupts require separate device measurements.
- Original SHA256 remains
  `c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1`.
  Existing sleep-candidate SHA256 remains
  `7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074`.
