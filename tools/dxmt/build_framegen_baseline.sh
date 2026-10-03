#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_dir=${1:-"$repo_root/build/step10-framegen"}

cmake -S "$repo_root" -B "$build_dir" -G Ninja
cmake --build "$build_dir" --verbose
ctest --test-dir "$build_dir" --output-on-failure
