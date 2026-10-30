# K64325 firmware tools

> [!CAUTION]
> **NO WARRANTY. NO SUPPORT. This is a barely-tested, vibe-coded procedure.**
>
> The tools and patch were developed with AI assistance and tested on **one
> physical trackball**. Offline checks do not establish safety on your device.
> Use entirely at your own risk. **If things break, you get to keep the pieces.**
> See the [MIT license](LICENSE) for the warranty and liability disclaimer.

Disable the approximately 20-second sensor deep sleep on the **wired Kensington
Expert Mouse K64325**, and select CPI and USB polling settings. Every generated
profile disables automatic sleep2 and keeps sleep1.

The tools read firmware from your device, patch that copy, and flash the changed
application pages. They run on Linux using Python's standard library.

**Already have an original backup? Go to [Flash a profile](#flash-a-profile).**

## Compatibility and risk

This project supports one original V0114 image, tested on one K64325 with an
SG8F7581 controller and sensor board marked `MS-6070U-3205_Sensor`.
See the [board photographs](docs/hardware.md).

Before entering the loader, check
[Kensington's serial-number eligibility list](https://www.kensington.com/en-gb/internal-support-pages/k64325-expert-mouse-wired-support-page/).
**Stop if your unit is eligible for V0116, even if it currently reports `0114`.**
That revision uses a different sensor data pin. Wireless Expert Mouse and
SlimBlade models are also outside the supported hardware.

> [!WARNING]
> Entering the loader rewrites persistent boot metadata, including when taking
> a backup. An incompatible unit can become stuck in loader mode and require
> external programming or replacement. Unplugging preserves that state.
> After flashing, a firmware startup failure can also prevent software recovery.

The tools check these values:

| Check | Required value |
| --- | --- |
| Application USB identity | `047d:1020`, `bcdDevice=0114` |
| Loader USB identity | `1c4f:8851`, `bcdDevice=0100` |
| USB and HID descriptors | Exact layouts for the selected profile |
| Original backup | Two matching 16,384-byte reads with the SHA-256 below |
| Loader code | Known fingerprint |

```text
c916f7f222b2991adad2816d467868ee73c828e48c17299270a84de5d2bcd6d1
```

Image and loader hashes become available after entry and readback. The original
backup contains the entry-modified metadata page at `0x0b00–0x0bff`; the loader
and application bytes are preserved.

## Requirements

- Linux with hidraw and sysfs, and Python **3.11 or later**.
- Device and lock-file access. The examples use `sudo`.
- A keyboard or another pointer while the trackball is in loader mode.

Run commands from this checkout. Close other firmware tools and keep the
trackball connected to the same USB port on the same computer. Backups are tied
to that port and sysfs path. The guided tools require an interactive terminal
and exactly one matching device.

## Take the original backup once

```sh
sudo tools/backup-firmware
```

The tool finds the device, prompts to enter the loader, reads the entire flash
twice, checks that the reads match, and prompts to return to application mode.
Type the displayed action and port at each prompt, such as `enter 3-2.1`.

It prints the backup directory:

```text
work/backup-TIMESTAMP-RANDOM/backup/
  dump-1.bin
  dump-2.bin
  manifest.json
```

Keep these three files together and save a separate copy. Use this original
backup for future builds and installations. A backup taken after patching is
a snapshot of the modified firmware.

## Flash a profile

Set `BACKUP` to your existing original backup directory. For example:

```sh
BACKUP=work/original
sudo tools/flash-firmware --backup "$BACKUP" --cpi 1600 --poll-rate-hz 250
```

This command:

1. Builds the selected profile, or verifies and reuses a matching cached build.
2. Prompts to enter the loader, unless the device is already there.
3. Prompts to install, saves the current firmware, and writes the changed pages.
4. Checks the written pages and two complete readbacks.
5. Prompts to exit, starts the firmware, and checks the application descriptors.

Confirm `enter <port>`, `install <port>`, and `exit <port>` when prompted.
Progress messages identify reading, writing, and verification. Success ends
with the device back in application mode.

### Settings

| Option | Values |
| --- | --- |
| `--cpi` | `stock`, `600`, `800`, `1000`, `1200`, `1600` |
| `--poll-rate-hz` | `100`, `250`, `500`, `1000` |
| `--out-root` | Build and transaction directory; default `work` |
| `--force-build` | Archive the cached build and generate it again |

CPI labels select sensor register values from the PAW3205DB-TJ3T map; the exact
sensor variant is unidentified. Measure effective sensitivity with
[`measure-motion`](#measure-motion).

The poll-rate settings request USB intervals of 10, 4, 2, or 1 ms. Linux xHCI
schedules the 10 ms setting at 8 ms, giving 125 polling opportunities per second.
Intervals below 10 ms are experimental for this low-speed USB device. See
[polling experiments](#polling-experiments) for timing details.

Builds are stored under names such as
`work/k64325-v0114-no-sleep2-cpi-1600-poll-4ms/`. Each flash gets a new
`work/flash-TIMESTAMP-RANDOM/` directory containing:

```text
workflow.jsonl
install/
  journal.jsonl
  before/
  after/
```

Keep the logs and readbacks. `after/` contains the verified installed image;
`before/` records the starting state. The installer preserves loader and
metadata pages and writes changed pages from `0x1f00`, `0x2200`, and `0x2e00`.

After flashing, check motion, buttons, scrolling, response after 60 seconds
idle, and suspend/resume.

## Build a CPI/polling profile

To build a file separately from flashing:

```sh
mkdir -p work
python3 tools/k64325_build.py \
  --input "$BACKUP/dump-1.bin" \
  --cpi 1600 --poll-rate-hz 250
```

The profile directory contains the named `.bin` and a manifest with hashes and
byte changes. The standalone builder requires a new profile directory. The
guided flasher reuses an existing matching build; use `--force-build` to archive
and rebuild it.

Install with `tools/flash-firmware` using the same settings and output root.
Use these tools rather than a generic full-image flasher: generated images
include the boot metadata captured during backup.

## Polling experiments

Changing the polling setting changes the endpoint descriptor's `bInterval`.
The clocks, timers, and sensor configuration stay the same when CPI is held
constant.

| Setting | Requested interval | Linux xHCI polling interval |
| --- | --- | --- |
| `100` | 10 ms | 8 ms / 125 Hz |
| `250` | 4 ms | 4 ms / 250 Hz |
| `500` | 2 ms | 2 ms / 500 Hz |
| `1000` | 1 ms | 1 ms / 1,000 Hz |

The firmware suppresses unchanged input. Sensor acquisition and firmware
processing also affect delivered report cadence. Button debounce and recovery
use loop counts, so their elapsed times can change with workload. Measure each
profile during movement and check buttons, scrolling, idle behavior, and resume.
See [resolution and rates](docs/firmware.md#how-resolution-and-rates-interact).

## Measure motion

```sh
sudo tools/measure-motion
```

Move the ball during the displayed window. Changing direction is fine for this
timing test. The summary reports event cadence, movement counts, and agreement
between raw HID and kernel input events. If kernel events are unavailable, it
labels the HID-arrival timing as approximate. Captures are saved under
`work/measure-TIMESTAMP-RANDOM/`.

For an approximate CPI measurement:

```sh
sudo tools/measure-motion --turns 2
```

Roll two complete turns in one direction about a fixed axis, then hold still.
Finish within the movement window. The estimate uses the nominal 55 mm ball
diameter and assumes a full circumference passes the sensor each turn. The
rotation axis affects that distance. If you have measured the surface travel,
supply it with `--surface-distance-mm`.

Calibration accepts a net displacement of at least 95% of the recorded path
and at most 1% backward projection. Clipping, substantial backtracking, or
capture errors suppress the CPI estimate. Timing remains available as shown
in the summary. Display pixels and cursor travel are separate from sensor CPI.

## Rollback

To return to stock CPI and polling while keeping the sleep fix:

```sh
sudo tools/flash-firmware --backup "$BACKUP" --cpi stock --poll-rate-hz 100
```

To restore the original application, including its sleep behavior, use the
lower-level rollback command. First inspect the device:

```sh
python3 tools/k64325_backup.py inspect
```

Set `PORT` and `APP` from its output. If the application is running, enter the
loader:

```sh
PORT=3-2.1          # replace with the reported port
APP=/dev/hidrawN   # replace with the reported application node
sudo python3 tools/k64325_backup.py enter \
  --device "$APP" --expect-port "$PORT" --confirm-marker-writes
```

Set `BOOT` to the loader node reported by entry or `inspect`, then restore:

```sh
BOOT=/dev/hidrawN  # replace with the reported loader node
sudo python3 tools/k64325_install.py rollback \
  --device "$BOOT" --expect-port "$PORT" \
  --backup "$BACKUP" --target original --out work/rollback-1 \
  --confirm-application-writes
```

Use a new output directory for each attempt. After successful verification,
the installer prints the exit command. Run it with the same permissions; it
uses `work/rollback-1/after` to check the installed firmware before starting it.

Rollback can restore partially written application pages when the loader is
accessible. It preserves the boot metadata created by entry.

## Errors and recovery

- **Permission denied:** use `sudo` or arrange access to hidraw and
  `/run/lock/k64325-workflow.lock`. Files created with `sudo` are private to root;
  use `sudo chown -R "$(id -u):$(id -g)" DIRECTORY` to read them as your user.
- **Existing output directory:** choose a new directory for a lower-level
  operation. For a cached build, reuse it or select `--force-build`.
- **Identity, port, or hash mismatch:** stop and check the selected device,
  backup, and profile against [compatibility](#compatibility-and-risk).
- **Write failure, timeout, or disconnect:** keep the logs and original backup.
  Run `python3 tools/k64325_backup.py inspect` before further commands. A failed
  program operation can leave flash-control state unclean. For a recovery
  attempt, reconnect on the same port to start a fresh loader session, inspect
  it, then use [rollback](#rollback) with a new transaction directory. Exit only
  after verification succeeds.
- **Neither loader nor application appears:** preserve the files for diagnosis.
  Recovery requires a method outside these USB tools.

## Tools and checks

| Tool | Purpose |
| --- | --- |
| `tools/backup-firmware` | Guided entry, backup, and exit |
| `tools/flash-firmware` | Build/cache selection, installation, verification, and exit |
| `tools/measure-motion` | Raw motion counts, timing, and CPI estimation |
| `tools/k64325_backup.py` | Individual inspect, enter, dump, and exit commands |
| `tools/k64325_build.py` | Build a named CPI/polling profile |
| `tools/k64325_patch_sleep.py` | Generate `candidate.bin` in an explicit output directory |
| `tools/k64325_install.py` | Install or roll back while already in loader mode |

Each tool has `--help`. See [firmware and protocol notes](docs/firmware.md) for
the patch, wire protocol, and instruction-level checks.

## License and firmware

Scripts and documentation are [MIT licensed](LICENSE), including its warranty
and liability disclaimer. Vendor firmware remains the vendor's property; obtain
it by reading your own device. Keep firmware dumps, patched images, and vendor
updaters out of commits, releases, and issue attachments. Generated files belong
under the Git-ignored `work/` directory.
