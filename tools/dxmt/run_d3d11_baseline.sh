#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 DISPOSABLE_ENGINE_COPY DISPOSABLE_PREFIX D3D11_APP_EXE" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
. "$script_dir/path_safety.sh"
engine_root=$(CDPATH= cd -- "$1" && pwd -P)
prefix_root=$(dxmt_canonical_path "$2")
app_path=$(dxmt_canonical_path "$3")
temp_root=$(dxmt_canonical_path "${TMPDIR:-/tmp}")
wine_path=$(dxmt_canonical_path "$engine_root/bin/wine")
wineserver_path=$(dxmt_canonical_path "$engine_root/bin/wineserver")

if dxmt_is_installed_engine_path "$engine_root"; then
  echo "refusing to launch from an installed application or Highball engine" >&2
  exit 2
fi
if ! dxmt_is_temporary_path "$engine_root" "$temp_root"; then
  echo "engine must be a disposable copy under a temporary directory" >&2
  exit 2
fi

if ! dxmt_is_temporary_path "$prefix_root" "$temp_root"; then
  echo "prefix must be disposable and located under a temporary directory" >&2
  exit 2
fi
if [ ! -d "$(dirname -- "$prefix_root")" ]; then
  echo "prefix parent must already exist: $(dirname -- "$prefix_root")" >&2
  exit 2
fi

if ! dxmt_is_within_root "$wine_path" "$engine_root" ||
   ! dxmt_is_within_root "$wineserver_path" "$engine_root"; then
  echo "Wine executables must resolve inside the disposable engine copy" >&2
  exit 2
fi
if [ ! -x "$wine_path" ] || [ ! -x "$wineserver_path" ] || [ ! -f "$app_path" ]; then
  echo "missing Wine executable, wineserver, or D3D11 app" >&2
  exit 2
fi
if [ -e "$prefix_root" ] || [ -L "$2" ] || [ -L "$prefix_root" ]; then
  echo "refusing to reuse an existing Wine prefix: $prefix_root" >&2
  exit 2
fi
if ! mkdir "$prefix_root"; then
  echo "could not reserve the fresh Wine prefix: $prefix_root" >&2
  exit 2
fi

log_root=$(mktemp -d "${TMPDIR:-/tmp}/dxmt-baseline-run.XXXXXXXX")

export WINEPREFIX="$prefix_root"
export WINEARCH=win64
export WINELOADER="$wine_path"
export WINESERVER="$wineserver_path"
export PATH="$engine_root/bin:$PATH"
export WINEDLLOVERRIDES='d3d10core=b;d3d11=b;dxgi=b;winemetal=b'
export WINEDEBUG=+loaddll
export DXMT_LOG_LEVEL=info
export DXMT_LOG_PATH="$log_root"

set +e
"$wine_path" "$app_path" >"$log_root/wine-console.log" 2>&1
app_status=$?
set -e

printf 'app_exit=%s\nlogs=%s\n' "$app_status" "$log_root"
exit "$app_status"
