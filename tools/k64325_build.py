#!/usr/bin/env python3
"""Build hash-pinned V0114 sleep/CPI/polling profiles offline."""

import argparse
import sys
from pathlib import Path

import k64325_patch_sleep as patch

POLL_INTERVALS_MS = {100: 10, 250: 4, 500: 2, 1000: 1}
RATE_WARNING = (
    "Poll rates are nominal bInterval requests: 100 Hz selects 10 ms, "
    "not a measured Linux report rate."
)
WARNINGS = (
    *patch.WARNINGS,
    patch.POLLING_WARNING,
    patch.CPI_WARNING,
    RATE_WARNING,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Build a hash-pinned V0114 automatic-sleep2 profile offline. No USB access."
        ),
        epilog="\n".join(f"WARNING: {warning}" for warning in WARNINGS),
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
        help="stock ID-dependent configuration or requested experimental CPI override",
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
    args = parser.parse_args(argv)
    interval = POLL_INTERVALS_MS[args.poll_rate_hz]
    cpi = None if args.cpi == "stock" else int(args.cpi)
    stem = f"k64325-v0114-no-sleep2-cpi-{args.cpi}-poll-{interval}ms"
    directory = args.out_root / stem
    try:
        source = patch.load_source(args.input)
        candidate, manifest = patch.prepare(source, args.input, interval, cpi)
        manifest["output"]["file"] = f"{stem}.bin"
        patch.write_output(directory, candidate, manifest)
    except (OSError, patch.PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for warning in (*manifest["warnings"], RATE_WARNING):
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"offline_candidate: {directory / manifest['output']['file']}")
    print(f"manifest: {directory / 'manifest.json'}")
    print(f"source SHA256: {manifest['source']['sha256']}")
    print(f"output SHA256: {manifest['output']['sha256']}")
    print(f"Selected CPI: {args.cpi}")
    print(f"USB descriptor bInterval: {interval} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
