# K64325 automatic deep-sleep patch

> [!CAUTION]
> **NO WARRANTY. This is a barely-tested, vibe-coded procedure.**
>
> The tools and patch were developed with AI assistance and tested on **one
> physical trackball**. Offline checks do not establish safety on your device.
> Use entirely at your own risk. **If things break, you get to keep the pieces.**
> See the [MIT license](LICENSE) for the warranty and liability disclaimer.

> [!WARNING]
> **Even attempting the backup can disable an incompatible device.**
>
> Reading firmware requires first entering the bootloader with a command that
> modifies persistent flash. An incompatible device may become stuck in update
> mode with **no known software recovery**; external programming or replacement
> may be necessary. Unplugging does not undo the boot-entry markers.
>
> **The firmware-hash checks happen after entry and readback. They cannot prevent
> this risk. “Read the firmware to check compatibility” is not a risk-free test.**
> Matching USB descriptors does not prove that an unknown firmware implements
> the same entry, read, or exit commands. The tools refuse the analyzed exit
> procedure if the dumped loader hash is unknown.
>
> In the analyzed firmware, entry erases the **boot-marker metadata page, not
> the bootloader code**. The dump command itself reads flash. The initial
> `inspect` command only reads sysfs and sends no device command.

Disable the approximately 20-second optical-sensor deep sleep on one specific
V0114 firmware image for the **wired Kensington Expert Mouse K64325**.

The procedure reads firmware from your trackball, patches that local copy, and
rewrites two application flash pages for the sleep fix. Optional polling
profiles also change the USB descriptor page. It uses Linux hidraw/sysfs and
Python's standard library. No vendor updater, firmware download, or firmware
binary is included.

Every profile disables automatic **sleep2** and keeps sleep1. Stock CPI leaves
the original sensor configuration unchanged. Choose CPI and polling interval
with the [profile builder](#build-a-cpipolling-profile); shorter polling
intervals and CPI overrides are experimental.
See [firmware notes](docs/firmware.md) for the patch and protocol details.

## Compatibility and risk

This was tested on **one K64325** with an SG8F7581 controller and a sensor board
marked `MS-6070U-3205_Sensor`. The exact sensor package marking was not confirmed.
The owner reported working motion, no sensor shutdown after one minute idle,
working suspend/resume, and working bootloader re-entry and exit after patching.

| Check | Required value |
| --- | --- |
| Application USB identity | `047d:1020`, `bcdDevice=0114` |
| Bootloader USB identity | `1c4f:8851`, `bcdDevice=0100` |
| USB and HID descriptors | Exact layouts checked by the tools |
| Original readback | 16,384 bytes, SHA-256 below |
| Loader code | Exact fingerprint checked by backup exit and installer |

**Matching the model name or `0114` alone is insufficient.** The patcher and
installer accept only this original readback SHA-256:

```text
c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1
```

This hash describes the image **after entering the bootloader**. Entry erases
metadata page `0x0b00–0x0bff` and writes persistent boot markers. The backup
preserves application and loader bytes, but cannot recover that page's contents
from before entry.

**Entry itself changes flash.** Before entry, USB descriptors cannot prove which
loader is installed. A different loader can leave the device in update mode:
the tools refuse to use the analyzed exit procedure with an unknown loader hash.
Unplugging does not clear the boot marker.

The installer preserves loader and metadata pages. It leaves the device in
bootloader mode so you can choose between exit and rollback. After exit, a
broken application can prevent software re-entry and therefore prevent software
rollback. Correct readback does not prove that an application will run.

**Do not use this procedure on a unit whose serial number is eligible for
Kensington's V0116 upgrade, even if it currently reports `0114`.** Check
[Kensington's serial-number eligibility list](https://www.kensington.com/en-gb/internal-support-pages/k64325-expert-mouse-wired-support-page/)
before entering the bootloader. Such units are outside this project's supported
hardware target. The analyzed V0116 firmware uses sensor data pin P0.2, whereas
the V0114 image supported here uses P0.1; the applications are not interchangeable.
Being outside Kensington's list does not by itself establish compatibility
with this patch: all descriptor and image-hash checks still apply.

The inspection tool can recognize V0116 descriptors, but the patcher and
installer do not accept V0116 firmware. Wireless Expert Mouse and SlimBlade
models are also outside this procedure.

## Requirements

- Linux with hidraw and sysfs.
- Python **3.11 or later**; no pip packages are required.
- Permission to open the trackball's hidraw node. Commands below use `sudo` for
  device access; omit it if your account already has access.
- Another working pointer or keyboard-only navigation: the trackball stops
  providing mouse input while in bootloader mode.

Run all commands from this checkout. Close other software that might send
commands to the trackball, and keep it connected to the same physical USB port
on the same computer throughout the procedure. Saved backups are tied to that
port and its sysfs path. The device lock is advisory.

Keep outputs under `work/`, which Git ignores:

```sh
python3 --version
sudo python3 --version
mkdir -p work
```

Check that both Python commands meet the version requirement if using `sudo`.
All output directories named below must be **new**. Tools refuse to overwrite
existing outputs. Do not chain entry, installation, and exit into an unattended
command sequence.

## 1. Identify the application

```sh
python3 -B tools/k64325_backup.py inspect
```

Inspection reads sysfs only. Find the line with `047d:1020`, `bcdDevice=0114`,
and `application: exact descriptors/identity match`.

Set these variables from that line. Replace both placeholders:

```sh
APP=/dev/hidrawN
PORT=...
```

`PORT` is the physical-port string printed after `port=`, such as `3-2.1`.
It is not the USB device number. A descriptor mismatch is a reason to stop,
not a reason to remove a check.

## 2. Enter the bootloader

This erases/writes the metadata page described above:

```sh
sudo python3 -B tools/k64325_backup.py enter \
  --device "$APP" --expect-port "$PORT" \
  --confirm-marker-writes
```

Success prints `Observed boot node ...`. The mouse is now inactive. Set `BOOT`
to the **reported** hidraw node; it may differ from `APP`:

```sh
BOOT=/dev/hidrawN
python3 -B tools/k64325_backup.py inspect
```

Require `1c4f:8851`, `bcdDevice=0100`, and
`boot: exact descriptors/identity match` on the same port.
If entry errors or times out, run only `inspect` next; do not repeat entry.

## 3. Download a double backup from the device

```sh
sudo python3 -B tools/k64325_backup.py dump \
  --device "$BOOT" --expect-port "$PORT" \
  --out work/original
sudo chown -R "$(id -u):$(id -g)" work/original
```

The dump reads all 16 KiB twice, using reverse chunk order on the second pass.
Require `Matching 16384-byte dumps saved` and the original SHA-256 above.
The directory contains `dump-1.bin`, `dump-2.bin`, and `manifest.json`.
Keep all three together and keep a separate copy somewhere safe.

If the dump fails, preserve its partial files and stop. If both dumps match but
their hash is unsupported, do not patch or install. To return to the existing
application, the following command checks both dumps, the loader fingerprint,
and current flash before changing the boot marker:

```sh
sudo python3 -B tools/k64325_backup.py exit \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/original --confirm-marker-writes
```

Use that command only when abandoning the procedure **before installation**.
An unknown loader or incomplete backup causes refusal; there is no override.

## 4. Patch the local readback

With the trackball still in bootloader mode:

```sh
python3 -B tools/k64325_build.py \
  --input work/original/dump-1.bin \
  --cpi stock --poll-rate-hz 100
```

The builder checks the entire source hash, the hook bytes, and unused insertion
space. It creates the deterministically named image
`work/k64325-v0114-no-sleep2-cpi-stock-poll-10ms/k64325-v0114-no-sleep2-cpi-stock-poll-10ms.bin`
and `manifest.json` in the same directory.
The candidate SHA-256 must be:

```text
7c1bd5139abaf5b7100b4779b228438361804aae82e595e2f19e3c3d4038a074
```

Its manifest status is `offline_candidate`: creating a file does not validate
it on another device. The full image retains the entry-modified metadata. **Do
not write it with a generic full-image flasher.** The installer writes only
changed pages from its allowlist: `0x1f00`, `0x2200`, and `0x2e00`. The default
sleep fix changes only the last two.

## 5. Install and verify

```sh
sudo python3 -B tools/k64325_install.py install \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/original \
  --candidate work/k64325-v0114-no-sleep2-cpi-stock-poll-10ms/k64325-v0114-no-sleep2-cpi-stock-poll-10ms.bin \
  --out work/install-1 \
  --confirm-application-writes
```

The installer:

1. Revalidates the original double backup, candidate, descriptors, and open device.
2. Saves two full current readbacks and requires equality with the original or
   one of the supported sleep/CPI/polling images.
3. Rewrites page `0x2e00` before page `0x2200`, then descriptor page `0x1f00`,
   skipping pages already matching the target. It preserves other bytes in
   each page and checks full-page erasure and full-page programmed contents.
4. Saves two full final readbacks and compares them with the exact candidate.

Require `install: two full readbacks match target SHA-256` followed by the
candidate hash. Success names `work/install-1/after` as the backup for exit.

```sh
sudo chown -R "$(id -u):$(id -g)" work/install-1
```

Keep the entire transaction directory. It includes `journal.jsonl`, an audit-only
`before/` snapshot, and the verified `after/` backup. Each erase/program intent
is synced to disk before transmission. The `before/` snapshot is deliberately
rejected by the exit command: it may describe a partial application during
rollback.

On any installation error, **do not exit and do not repeat installation**.
See [errors and recovery](#errors-and-recovery).

## 6. Exit and test

Exit is a separate decision. Use the verified **installed** backup:

```sh
sudo python3 -B tools/k64325_backup.py exit \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/install-1/after \
  --confirm-marker-writes
```

This rereads current flash before clearing the boot marker. It does not undo
the sensor patch. The application still advertises `bcdDevice=0114`.

Check:

- Motion, buttons, and scrolling work.
- After 60 seconds without ball movement, motion responds without the previous
  deep-sleep behavior.
- The same idle test passes after computer suspend/resume.
- Bootloader re-entry works: repeat identification and entry, update `BOOT` to
  the reported node, then repeat the exit command above using
  `work/install-1/after`.

If exit errors or times out, inspect the current enumeration; do not repeat the
exit command blindly.

## Build a CPI/polling profile

Build offline from the **original** readback, even when a patched image is
installed. This command neither opens a device nor installs firmware:

```sh
mkdir -p work
python3 -B tools/k64325_build.py \
  --input work/original/dump-1.bin \
  --cpi 1600 --poll-rate-hz 250
```

This creates:

```text
work/k64325-v0114-no-sleep2-cpi-1600-poll-4ms/
  k64325-v0114-no-sleep2-cpi-1600-poll-4ms.bin
  manifest.json
```

- `--cpi`: `stock`, `600`, `800`, `1000`, `1200`, or `1600`.
  `stock` does not alter the original CPI instructions: the firmware chooses
  600 or 800 according to sensor ID under the cited sensor map. An explicit
  number selects that setting for both accepted IDs.
- `--poll-rate-hz`: `100`, `250`, `500`, or `1000`, corresponding to descriptor
  intervals of 10, 4, 2, or 1 ms. These are nominal requests, not measured report
  rates. Linux xHCI rounds the original 10 ms request to 8 ms / 125 Hz.
- `--out-root`: an existing output parent, default `work`. The profile directory
  and binary basename are derived from the settings; an existing profile
  directory is refused, never overwritten. Reuse the image or choose a different
  output parent for a separate build.

All 24 combinations include the sleep fix. The filename records the requested
CPI and descriptor interval; the manifest records the source/output hashes and
byte changes. The sensor variant and resulting CPI are unconfirmed. See
[CPI and rate interactions](docs/firmware.md#how-resolution-and-rates-interact)
before comparing profiles.

Install the generated `.bin` with the guarded installer from step 5, substituting
its path for `--candidate` and choosing a new transaction directory. Keep the
original double backup as `--backup`; use the successful transaction's `after/`
backup for exit. A build manifest does not replace a device backup.

## Polling experiments

These profiles retain the sleep fix and change only USB `bInterval` at image
offset `0x1f36`. USB speed, clocks, timers, and sensor settings remain unchanged.

| Builder `--poll-rate-hz` | Requested interval | Nominal polling rate |
| --- | --- | --- |
| `100` | 10 ms | 100 Hz; Linux rounds this to 8 ms / 125 Hz |
| `250` | 4 ms | 250 Hz |
| `500` | 2 ms | 500 Hz |
| `1000` | 1 ms | 1,000 Hz |

**Shortened intervals are experimental and nonstandard for low-speed USB.**
[Linux accepts them](https://github.com/torvalds/linux/blob/master/drivers/usb/core/config.c),
but other hosts may not. These are polling requests, not verified fresh-motion
rates. The firmware suppresses reports when inputs have not changed. Sensor
sampling and firmware execution time can limit throughput; button debounce
and recovery use call counts, so their timing may change with workload.

Start with 4 ms. Generate it from the **original** readback, even if the sleep
fix is already installed:

```sh
python3 -B tools/k64325_build.py \
  --input work/original/dump-1.bin \
  --cpi stock --poll-rate-hz 250
```

The [profile hashes](docs/firmware.md#optional-polling-profiles) identify each
complete candidate. Keep using `work/original` as the installer's backup input;
do not replace it with a backup of a patched image.

If the application is running, repeat identification and bootloader entry
above, updating `APP` and `BOOT` from the reported nodes. Then install:

```sh
sudo python3 -B tools/k64325_install.py install \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/original \
  --candidate work/k64325-v0114-no-sleep2-cpi-stock-poll-4ms/k64325-v0114-no-sleep2-cpi-stock-poll-4ms.bin \
  --out work/install-4ms-1 --confirm-application-writes
```

Require two full readbacks matching the selected profile's hash. Switching
between profiles that differ only in polling writes only the descriptor page.
Changing CPI also changes page `0x2200`. On
error, keep the transaction and follow [errors and recovery](#errors-and-recovery).

Only after success, use this transaction's verified backup for exit:

```sh
sudo chown -R "$(id -u):$(id -g)" work/install-4ms-1
sudo python3 -B tools/k64325_backup.py exit \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/install-4ms-1/after --confirm-marker-writes
```

The observed application must match the installed profile's descriptors; exit
prints its interval. Measure report intervals during continuous ball motion,
and repeat the motion/button/scroll, idle, suspend/resume, and re-entry checks.
For re-entry testing, exit using `work/install-4ms-1/after` rather than an older
transaction's backup.

To try 2 or 1 ms, select `--poll-rate-hz 500` or `1000` and use a new transaction
directory. To return to the tested sleep-only image, use rollback with
`--target sleep` as below.

## Rollback

Rollback requires the original `work/original` backup and an accessible,
matching bootloader on the same physical port/sysfs path. If the patched
application is running, first identify it and enter the bootloader as above.
Use the current boot node in `BOOT`.

Choose the target explicitly:

- `--target sleep`: restore stock CPI while keeping the sensor fix. The default
  interval is the original 10 ms; add `--poll-interval-ms 4`, `2`, or `1` to keep
  a shorter polling interval while undoing a CPI override.
- `--target original`: restore the original application, removing both the
  sleep fix and any CPI/polling changes. This is the default if `--target` is
  omitted and requires the default 10 ms interval argument.

For example, to undo a polling experiment while keeping the sleep fix:

```sh
sudo python3 -B tools/k64325_install.py rollback \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/original \
  --target sleep \
  --out work/rollback-1 \
  --confirm-application-writes
```

The original target restores page `0x2200` before `0x2e00`; the sleep target
restores `0x2e00` before `0x2200`. Both restore descriptor page `0x1f00` last and
skip pages already matching the target. Rollback accepts partially
erased/programmed contents inside these three pages, but refuses any change
elsewhere. It requires two full final reads matching the selected target's
SHA-256 before reporting success.

Only after that success:

```sh
sudo python3 -B tools/k64325_backup.py exit \
  --device "$BOOT" --expect-port "$PORT" \
  --backup work/rollback-1/after \
  --confirm-marker-writes
sudo chown -R "$(id -u):$(id -g)" work/rollback-1
```

Use the original backup as rollback input, not a transaction's `after/` backup.
Neither target can restore metadata erased before the initial backup.

## Errors and recovery

- **Permission denied:** fix hidraw access or use `sudo`; do not weaken identity
  checks. Files produced with `sudo` are private to root until ownership changes.
- **Existing output directory:** keep it. For a later, separately chosen
  operation, use a new name such as `work/rollback-2`; existing logs are not
  resume files.
- **Wrong identity, port, hash, or changed bytes outside the three allowed pages:** stop.
  There is no force mode. Do not edit a manifest to make validation pass.
- **Erase/program failure, timeout, or disconnect:** retain the transaction and
  original backup. Do not issue exit or immediately resend commands. A program
  failure can leave flash-control and interrupt state unclean. For an explicit
  recovery attempt, reconnect on the same port to start a fresh loader session,
  inspect its identity, and then use rollback with a new transaction directory.
  The persistent boot marker is outside the pages this installer writes.
- **No matching loader after reconnection, or no application/loader after exit:**
  these tools provide no alternate recovery interface. Preserve the files for
  diagnosis rather than sending commands to a guessed device.

## Tools and offline checks

| Tool | Purpose |
| --- | --- |
| `tools/k64325_backup.py` | Sysfs inspection, explicit entry, double readback, guarded exit |
| `tools/k64325_build.py` | CPI/polling selection with deterministic image names |
| `tools/k64325_patch_sleep.py` | Hash-pinned patch generator; explicit `--out` directory containing `candidate.bin` |
| `tools/k64325_install.py` | Guarded application-page installation and explicit rollback |

Each tool has `--help`. Syntax and help can be checked without a trackball:

```sh
mkdir -p work
PYTHONPYCACHEPREFIX="$PWD/work/pycache" python3 -m py_compile tools/*.py
for tool in tools/k64325_*.py; do
  python3 -B "$tool" --help >/dev/null || exit 1
done
```

The development checks included simulated sysfs/transport failures, partial
installation and rollback, and execution of the analyzed loader/patch bytes in
partial instruction models. Those models assume flash-controller behavior;
they are not independently validated hardware emulators. These historical
checks and the single-device results do not extend the supported image list.

## License and firmware

The scripts and documentation are [MIT licensed](LICENSE). Vendor firmware is
not distributed, and this license grants no rights to it. Obtain the input by
reading your own device as described above; do not submit firmware dumps,
patched images, or the vendor updater in commits, releases, or issue attachments.

`work/`, firmware files, and local analysis artifacts are ignored by Git.
Keep generated outputs there and inspect files before publishing; outputs saved
elsewhere may need additional ignore rules.
