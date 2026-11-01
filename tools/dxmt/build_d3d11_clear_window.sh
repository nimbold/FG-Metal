#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compiler=${MINGW_CXX:-x86_64-w64-mingw32-g++}
output=${1:-"$repo_root/build/r100-d3d11-harness/fgmetal-r100-d3d11-harness.exe"}

mkdir -p "$(dirname -- "$output")"
"$compiler" -std=c++17 -O2 -Wall -Wextra -Wpedantic -static-libgcc \
  -static-libstdc++ -static -mwindows "$repo_root/tools/dxmt/test_apps/d3d11_clear_window.cpp" \
  -o "$output" -ld3d11 -ld3dcompiler -luser32 -lkernel32

file "$output"
shasum -a 256 "$output"
