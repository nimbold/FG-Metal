#!/bin/sh
set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 DISPOSABLE_ENGINE_COPY DXMT_CROSS_BUILD_ROOT [LLVM15_RUNTIME_LIB_DIR]" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
. "$script_dir/path_safety.sh"
engine_root=$(CDPATH= cd -- "$1" && pwd -P)
build_root=$(CDPATH= cd -- "$2" && pwd -P)
temp_root=$(dxmt_canonical_path "${TMPDIR:-/tmp}")
llvm_runtime_lib_dir=""
if [ "$#" -eq 3 ]; then
  llvm_runtime_lib_dir=$(CDPATH= cd -- "$3" && pwd -P)
  for library in libc++.1.dylib libc++abi.1.dylib libunwind.1.dylib; do
    if [ ! -f "$llvm_runtime_lib_dir/$library" ]; then
      echo "missing LLVM 15 runtime library: $llvm_runtime_lib_dir/$library" >&2
      exit 2
    fi
  done
fi

if dxmt_is_installed_engine_path "$engine_root"; then
  echo "refusing to stage into an installed application or Highball engine" >&2
  exit 2
fi
if ! dxmt_is_temporary_path "$engine_root" "$temp_root"; then
  echo "engine must be a disposable copy under a temporary directory" >&2
  exit 2
fi

windows_dir="$engine_root/lib/wine/x86_64-windows"
unix_dir="$engine_root/lib/wine/x86_64-unix"
for relative_path in \
  src/d3d10/d3d10core.dll \
  src/d3d11/d3d11.dll \
  src/dxgi/dxgi.dll \
  src/winemetal/winemetal.dll \
  src/winemetal/unix/winemetal.so
do
  if [ ! -f "$build_root/$relative_path" ]; then
    echo "missing DXMT build output: $build_root/$relative_path" >&2
    exit 2
  fi
done

for directory in \
  "$engine_root/lib" \
  "$engine_root/lib/wine" \
  "$windows_dir" \
  "$unix_dir"
do
  if [ -L "$directory" ]; then
    echo "refusing to stage through a symlinked engine directory: $directory" >&2
    exit 2
  fi
done

if [ ! -d "$windows_dir" ] || [ ! -d "$unix_dir" ] || [ ! -x "$engine_root/bin/wine" ]; then
  echo "engine copy does not have the expected x86_64 Wine install layout" >&2
  exit 2
fi

for target in \
  "$windows_dir/d3d10core.dll" \
  "$windows_dir/d3d11.dll" \
  "$windows_dir/dxgi.dll" \
  "$windows_dir/winemetal.dll" \
  "$unix_dir/winemetal.so"
do
  if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
    echo "refusing to replace a non-regular runtime target in the engine copy: $target" >&2
    exit 2
  fi
done
if [ "$#" -eq 3 ]; then
  for library in libc++.1.dylib libc++abi.1.dylib libunwind.1.dylib; do
    target="$unix_dir/$library"
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
      echo "refusing to replace a non-regular runtime target in the engine copy: $target" >&2
      exit 2
    fi
  done
fi

stage_dir=""
installed_names=""
install_started=0
install_complete=0
rollback() {
  for name in $installed_names; do
    case "$name" in
      d3d10core.dll) target="$windows_dir/d3d10core.dll" ;;
      d3d11.dll) target="$windows_dir/d3d11.dll" ;;
      dxgi.dll) target="$windows_dir/dxgi.dll" ;;
      winemetal.dll) target="$windows_dir/winemetal.dll" ;;
      winemetal.so) target="$unix_dir/winemetal.so" ;;
      libc++.1.dylib) target="$unix_dir/libc++.1.dylib" ;;
      libc++abi.1.dylib) target="$unix_dir/libc++abi.1.dylib" ;;
      libunwind.1.dylib) target="$unix_dir/libunwind.1.dylib" ;;
      *) continue ;;
    esac
    if [ -f "$stage_dir/backup/$name" ]; then
      if ! mv -f "$stage_dir/backup/$name" "$target"; then
        echo "failed to restore previous runtime file after staging error: $target" >&2
      fi
    elif ! rm -f "$target"; then
      echo "failed to remove partially staged runtime file: $target" >&2
    fi
  done
}
cleanup() {
  status=$?
  if [ "$install_started" -eq 1 ] && [ "$install_complete" -ne 1 ]; then
    rollback
  fi
  if [ -n "$stage_dir" ]; then
    rm -rf "$stage_dir"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stage_dir=$(mktemp -d "$engine_root/.dxmt-stage.XXXXXXXX")
mkdir "$stage_dir/backup"
stage_file() {
  source=$1
  target=$2
  name=$3
  cp -p "$source" "$stage_dir/$name"
  if [ -e "$target" ]; then
    cp -p "$target" "$stage_dir/backup/$name"
  fi
}

stage_file "$build_root/src/d3d10/d3d10core.dll" "$windows_dir/d3d10core.dll" d3d10core.dll
stage_file "$build_root/src/d3d11/d3d11.dll" "$windows_dir/d3d11.dll" d3d11.dll
stage_file "$build_root/src/dxgi/dxgi.dll" "$windows_dir/dxgi.dll" dxgi.dll
stage_file "$build_root/src/winemetal/winemetal.dll" "$windows_dir/winemetal.dll" winemetal.dll
stage_file "$build_root/src/winemetal/unix/winemetal.so" "$unix_dir/winemetal.so" winemetal.so
if [ "$#" -eq 3 ]; then
  for library in libc++.1.dylib libc++abi.1.dylib libunwind.1.dylib; do
    stage_file "$llvm_runtime_lib_dir/$library" "$unix_dir/$library" "$library"
  done
fi

install_one() {
  name=$1
  target=$2
  install_started=1
  # Record before the atomic rename so a signal delivered immediately after
  # `mv` still makes cleanup restore this target.
  installed_names="$installed_names $name"
  mv -f "$stage_dir/$name" "$target"
}
install_one d3d10core.dll "$windows_dir/d3d10core.dll"
install_one d3d11.dll "$windows_dir/d3d11.dll"
install_one dxgi.dll "$windows_dir/dxgi.dll"
install_one winemetal.dll "$windows_dir/winemetal.dll"
install_one winemetal.so "$unix_dir/winemetal.so"
if [ "$#" -eq 3 ]; then
  install_one libc++.1.dylib "$unix_dir/libc++.1.dylib"
  install_one libc++abi.1.dylib "$unix_dir/libc++abi.1.dylib"
  install_one libunwind.1.dylib "$unix_dir/libunwind.1.dylib"
fi
shasum -a 256 \
  "$windows_dir/d3d10core.dll" \
  "$windows_dir/d3d11.dll" \
  "$windows_dir/dxgi.dll" \
  "$windows_dir/winemetal.dll" \
  "$unix_dir/winemetal.so"
if [ "$#" -eq 3 ]; then
  shasum -a 256 \
    "$unix_dir/libc++.1.dylib" \
    "$unix_dir/libc++abi.1.dylib" \
    "$unix_dir/libunwind.1.dylib"
fi
install_complete=1
