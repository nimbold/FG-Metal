#!/usr/bin/env python3
"""Fail closed if a Wine runner is not using the storage-managed lease."""
from __future__ import annotations

import json
import os
import sys
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from storage_policy import assert_prefix_lease  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prefix")
    parser.add_argument("runner", help="legacy runner source path to authorize against the manifest")
    args = parser.parse_args()
    record = assert_prefix_lease(args.prefix, os.environ.get("WINELOADER"), args.runner)
    print(json.dumps({
        "status": "PASS",
        "experiment_id": record["experiment_id"],
        "prefix_path": record["prefix_path"],
        "manifest_sha256": record["manifest_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
