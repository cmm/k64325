#!/usr/bin/env python3
"""Build hash-pinned V0114 sleep/CPI/polling profiles offline."""

import argparse
import sys
from pathlib import Path

import k64325_patch_sleep as patch

POLL_INTERVALS_MS = {100: 10, 250: 4, 500: 2, 1000: 1}
RATE_WARNING = (
    "Poll-rate settings select USB intervals: 100/250/500/1000 Hz request 10/4/2/1 ms."
)
WARNINGS = (
    "Automatic sleep2 is disabled; sleep1 stays enabled.",
    "Use tools/measure-motion to measure effective CPI and report delivery rate.",
    RATE_WARNING,
)


def profile_stem(cpi, poll_rate_hz, no_motion_gain=False):
    interval = POLL_INTERVALS_MS[poll_rate_hz]
    return f"k64325-v0114-no-sleep2-cpi-{cpi}-poll-{interval}ms" + (
        "-no-motion-gain" if no_motion_gain else ""
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=("Build a V0114 firmware profile with automatic sleep2 disabled."),
        epilog="\n".join(WARNINGS),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="exact V0114 dump; regular non-symlink file",
    )
    parser.add_argument(
        "--cpi",
        required=True,
        choices=("stock", *(str(cpi) for cpi in patch.CPI_CHOICES)),
        help="sensor CPI setting, or stock configuration",
    )
    parser.add_argument(
        "--poll-rate-hz",
        required=True,
        type=int,
        choices=tuple(POLL_INTERVALS_MS),
        help="nominal bInterval requests: 100/250/500/1000 Hz select 10/4/2/1 ms",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("work"),
        help=(
            "existing non-symlink directory (default: work); "
            "profile directory must not exist"
        ),
    )
    parser.add_argument(
        "--no-motion-gain", action="store_true", help=patch.MOTION_GAIN_HELP
    )
    args = parser.parse_args(argv)
    interval = POLL_INTERVALS_MS[args.poll_rate_hz]
    cpi = None if args.cpi == "stock" else int(args.cpi)
    stem = profile_stem(args.cpi, args.poll_rate_hz, args.no_motion_gain)
    directory = args.out_root / stem
    try:
        source = patch.load_source(args.input)
        candidate, manifest = patch.prepare(
            source, args.input, interval, cpi, args.no_motion_gain
        )
        manifest["output"]["file"] = f"{stem}.bin"
        patch.write_output(directory, candidate, manifest)
    except (OSError, patch.PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Firmware: {directory / manifest['output']['file']}")
    print(f"Manifest: {directory / 'manifest.json'}")
    print(f"CPI setting: {args.cpi}; USB interval: {interval} ms")
    print(f"Motion gain: {patch.motion_gain_label(args.no_motion_gain)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
