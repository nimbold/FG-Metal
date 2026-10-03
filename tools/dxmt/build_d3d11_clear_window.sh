#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compiler=${MINGW_CXX:-x86_64-w64-mingw32-g++}
output=${1:-"$repo_root/build/step10a-d3d11-clear-window/d3d11_clear_window.exe"}

mkdir -p "$(dirname -- "$output")"
"$compiler" -std=c++17 -O2 -Wall -Wextra -Wpedantic -static-libgcc \
  -static-libstdc++ -static -mwindows "$repo_root/tools/dxmt/test_apps/d3d11_clear_window.cpp" \
  -o "$output" -ld3d11 -luser32 -lkernel32

file "$output"
shasum -a 256 "$output"
