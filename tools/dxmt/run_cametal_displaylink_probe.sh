#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 OUTPUT_CSV" >&2
  exit 2
fi

probe_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/cametal_displaylink_probe.mm
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
. "$script_dir/path_safety.sh"
output_argument=$1
output_path=$(dxmt_canonical_path "$output_argument")
if [ -e "$output_path" ] || [ -L "$output_argument" ] || [ -L "$output_path" ]; then
  echo "refusing to overwrite an existing probe output: $output_path" >&2
  exit 2
fi
output_parent=$(dirname -- "$output_path")
mkdir -p "$output_parent"
output_path=$(dxmt_canonical_path "$output_path")
if [ -e "$output_path" ] || [ -L "$output_argument" ] || [ -L "$output_path" ]; then
  echo "refusing to overwrite an existing probe output: $output_path" >&2
  exit 2
fi
probe_tmp=$(mktemp -d "${TMPDIR:-/tmp}/framegen-cametal-displaylink.XXXXXXXX")
probe_bin="$probe_tmp/probe"
cleanup() {
  rm -rf "$probe_tmp"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

xcrun --sdk macosx clang++ -std=c++17 -fobjc-arc -mmacosx-version-min=14.0 \
  -framework AppKit -framework QuartzCore -framework Metal -framework CoreGraphics \
  "$probe_source" -o "$probe_bin"
"$probe_bin" "$output_path"
