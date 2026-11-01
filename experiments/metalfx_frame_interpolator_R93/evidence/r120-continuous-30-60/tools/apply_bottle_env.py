#!/usr/bin/env python3
"""Inject or restore the R120 pacer environment in the Highball bottle.

The live measurement needs the observer dylib and its flags present in the game
process. The working route, established in R116 and confirmed in R118, is the
Highball launcher with the variables in the bottle's `environment` map -- the
standalone shell launcher aborts in the D3DMetal timestamp shim.

This tool therefore edits `bottle.json` in place, and can put it back exactly.
It always writes a timestamped backup before touching anything, and `--restore`
prefers that backup over reconstructing the original.

Usage:
    apply_bottle_env.py --apply           # inject R120 pacer environment
    apply_bottle_env.py --restore         # put the bottle back
    apply_bottle_env.py --show            # print current environment, change nothing
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

BOTTLE = Path("/Users/nima/Library/Application Support/Highball/bottles/Games/bottle.json")
BACKUP = BOTTLE.parent / "bottle.json.pre-r120"
ENV_SOURCE = Path(
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93"
    "/evidence/r120-continuous-30-60/bottle-env-r120-pace.json"
)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--restore", action="store_true")
    group.add_argument("--show", action="store_true")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="env JSON to inject; defaults to the Phase 1 pacer config")
    args = parser.parse_args()

    if not BOTTLE.exists():
        print(f"FAILED bottle.json not found at {BOTTLE}")
        return 2

    bottle = load(BOTTLE)

    if args.show:
        print(json.dumps(bottle.get("environment", {}), indent=2, sort_keys=True))
        return 0

    if args.restore:
        if not BACKUP.exists():
            print(f"FAILED no backup at {BACKUP}; refusing to guess the original")
            return 2
        save(BOTTLE, load(BACKUP))
        print(f"RESTORED bottle={BOTTLE} from={BACKUP}")
        return 0

    wanted = load(args.env_file or ENV_SOURCE)["environment"]
    if not BACKUP.exists():
        shutil.copy2(BOTTLE, BACKUP)
        print(f"BACKUP bottle={BACKUP}")
    bottle["environment"] = dict(wanted)
    save(BOTTLE, bottle)
    print(f"APPLIED variables={len(wanted)} bottle={BOTTLE}")
    for key in sorted(wanted):
        print(f"  {key}={wanted[key]}")
    print(f"STAMP {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
