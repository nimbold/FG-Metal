#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 SOURCE_RUNTIME_ENGINE NEW_DISPOSABLE_ENGINE_COPY" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
. "$script_dir/path_safety.sh"
source_root=$(CDPATH= cd -- "$1" && pwd -P)
destination_argument=$2
destination_root=$(dxmt_canonical_path "$destination_argument")
temp_root=$(dxmt_canonical_path "${TMPDIR:-/tmp}")

if dxmt_is_installed_engine_path "$destination_root"; then
  echo "refusing a destination inside an installed application or Highball engine" >&2
  exit 2
fi
if ! dxmt_is_temporary_path "$destination_root" "$temp_root"; then
  echo "destination must be a disposable path under a temporary directory" >&2
  exit 2
fi
if [ "$source_root" = "$destination_root" ]; then
  echo "destination must be distinct from the source" >&2
  exit 2
fi
if [ -e "$destination_root" ] || [ -L "$destination_argument" ] || [ -L "$destination_root" ]; then
  echo "destination must be a new path distinct from the source" >&2
  exit 2
fi

source_framework_lib=""
destination_framework_lib=""
reuse_framework_lib=0
if [ -L "$source_root/lib/libinotify.0.dylib" ]; then
  source_framework_link="$source_root/lib/libinotify.0.dylib"
  source_framework_target=$(readlink "$source_framework_link")
  case "$source_framework_target" in
    /*)
      echo "refusing an absolute libinotify link that would escape the disposable copy" >&2
      exit 2
      ;;
  esac
  source_framework_lib="$(dirname -- "$source_root")/frameworks/libinotify.0.dylib"
  destination_framework_lib="$(dirname -- "$destination_root")/frameworks/libinotify.0.dylib"
  destination_framework_dir=$(dirname -- "$destination_framework_lib")
  if [ -L "$destination_framework_dir" ]; then
    echo "refusing a symlinked disposable framework directory: $destination_framework_dir" >&2
    exit 2
  fi
  resolved_source_framework_target=$(dxmt_canonical_path "$(dirname -- "$source_framework_link")/$source_framework_target")
  resolved_expected_framework=$(dxmt_canonical_path "$source_framework_lib")
  if [ "$resolved_source_framework_target" != "$resolved_expected_framework" ]; then
    echo "libinotify link does not resolve to the engine bundle's sibling framework: $source_framework_link" >&2
    exit 2
  fi
  if [ ! -f "$source_framework_lib" ]; then
    echo "missing Highball framework target: $source_framework_lib" >&2
    exit 2
  fi
  if [ -e "$destination_framework_lib" ] || [ -L "$destination_framework_lib" ]; then
    if [ -f "$destination_framework_lib" ] && [ ! -L "$destination_framework_lib" ] &&
       cmp -s "$source_framework_lib" "$destination_framework_lib"; then
      reuse_framework_lib=1
    else
      echo "refusing to overwrite a different framework file: $destination_framework_lib" >&2
      exit 2
    fi
  fi
fi

destination_parent=$(dirname -- "$destination_root")
mkdir -p "$destination_parent"
destination_root=$(dxmt_canonical_path "$destination_root")
if ! dxmt_is_temporary_path "$destination_root" "$temp_root"; then
  echo "destination resolved outside a temporary directory" >&2
  exit 2
fi
if [ -e "$destination_root" ] || [ -L "$destination_argument" ] || [ -L "$destination_root" ]; then
  echo "destination must be a new path distinct from the source" >&2
  exit 2
fi

framework_created=0
framework_temp=""
destination_created=0
copy_complete=0
cleanup() {
  status=$?
  if [ "$copy_complete" -ne 1 ]; then
    if [ "$destination_created" -eq 1 ]; then
      rm -rf "$destination_root"
    fi
    if [ -n "$framework_temp" ]; then
      rm -f "$framework_temp"
    fi
    if [ "$framework_created" -eq 1 ]; then
      rm -f "$destination_framework_lib"
      rmdir "$(dirname -- "$destination_framework_lib")" 2>/dev/null || true
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! mkdir "$destination_root"; then
  echo "destination must be a new path distinct from the source" >&2
  exit 2
fi
destination_created=1
ditto "$source_root" "$destination_root"

# Highball's engine keeps a relative symlink to libinotify beside the engine.
# Install that sibling only when it is absent; never replace another copy.
if [ -n "$source_framework_lib" ] && [ "$reuse_framework_lib" -eq 0 ]; then
  destination_framework_lib="$(dirname -- "$destination_root")/frameworks/libinotify.0.dylib"
  destination_framework_dir=$(dirname -- "$destination_framework_lib")
  mkdir -p "$destination_framework_dir"
  framework_temp=$(mktemp "$destination_framework_dir/.libinotify.XXXXXXXX")
  cp -p "$source_framework_lib" "$framework_temp"
  if ! ln "$framework_temp" "$destination_framework_lib"; then
    echo "could not reserve the disposable framework copy: $destination_framework_lib" >&2
    exit 2
  fi
  framework_created=1
  rm -f "$framework_temp"
  framework_temp=""
fi

copy_complete=1
printf 'Disposable engine copy: %s\n' "$destination_root"
