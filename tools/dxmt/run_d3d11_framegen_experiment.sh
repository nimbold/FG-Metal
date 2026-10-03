#!/bin/sh
set -eu

if [ "$#" -ne 8 ]; then
  echo "usage: $0 DISPOSABLE_ENGINE FRESH_PREFIX D3D11_APP OUTPUT_DIR SECONDS GENERATION(0|1) STATIC_TEXTURE(0|1) FAIL_STAGE_OR_NONE" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
. "$script_dir/path_safety.sh"
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)
engine_root=$(CDPATH= cd -- "$1" && pwd -P)
prefix_path=$(dxmt_canonical_path "$2")
app_source=$(dxmt_canonical_path "$3")
output_argument=$4
output_root=$(dxmt_canonical_path "$output_argument")
seconds=$5
generation=$6
static_texture=$7
fail_stage=$8
temp_root=$(dxmt_canonical_path "${TMPDIR:-/tmp}")
wine_path=$(dxmt_canonical_path "$engine_root/bin/wine")
wineserver_path=$(dxmt_canonical_path "$engine_root/bin/wineserver")

if dxmt_is_installed_engine_path "$engine_root"; then
  echo "refusing to run an installed application or Highball engine" >&2
  exit 2
fi
if ! dxmt_is_temporary_path "$engine_root" "$temp_root"; then
  echo "engine must be a disposable copy under a temporary directory" >&2
  exit 2
fi

if ! dxmt_is_temporary_path "$prefix_path" "$temp_root"; then
  echo "prefix must be a fresh disposable path under a temporary directory" >&2
  exit 2
fi
if [ ! -d "$(dirname -- "$prefix_path")" ]; then
  echo "prefix parent must already exist: $(dirname -- "$prefix_path")" >&2
  exit 2
fi

case "$seconds" in
  ''|*[!0-9]*) echo "SECONDS must be an integer from 1 through 1800" >&2; exit 2 ;;
esac
if [ "$seconds" -lt 1 ] || [ "$seconds" -gt 1800 ]; then
  echo "SECONDS must be an integer from 1 through 1800" >&2
  exit 2
fi
case "$generation:$static_texture" in
  0:0|0:1|1:0|1:1) ;;
  *) echo "GENERATION and STATIC_TEXTURE must each be 0 or 1" >&2; exit 2 ;;
esac
case "$fail_stage" in
  none|circuit-breaker|drawable|submit|generated-allocation|generation|\
  generated-completion|deadline|allocation|escrow-allocation|escrow-setup|\
  escrow-preflight|escrow-encode) ;;
  *) echo "unsupported failure injection stage: $fail_stage" >&2; exit 2 ;;
esac
lifecycle_test=${DXMT_FRAMEGEN_LIFECYCLE_TEST:-0}
case "$lifecycle_test" in
  0|1) ;;
  *) echo "DXMT_FRAMEGEN_LIFECYCLE_TEST must be 0 or 1" >&2; exit 2 ;;
esac

if ! dxmt_is_within_root "$wine_path" "$engine_root" ||
   ! dxmt_is_within_root "$wineserver_path" "$engine_root"; then
  echo "Wine executables must resolve inside the disposable engine copy" >&2
  exit 2
fi
if [ ! -x "$wine_path" ] ||
   [ ! -x "$wineserver_path" ] ||
   [ ! -f "$app_source" ] ||
   [ ! -f "$repo_root/tools/dxmt/analyze_framegen_run.py" ]; then
  echo "missing disposable Wine runtime, app, or analyzer" >&2
  exit 2
fi
if [ -e "$prefix_path" ] || [ -L "$2" ] || [ -L "$prefix_path" ]; then
  echo "refusing to reuse an existing Wine prefix: $prefix_path" >&2
  exit 2
fi
if [ -e "$output_root" ] || [ -L "$output_argument" ] || [ -L "$output_root" ]; then
  echo "refusing to overwrite an existing run directory: $output_root" >&2
  exit 2
fi

mkdir -p "$(dirname -- "$output_root")"
output_root=$(dxmt_canonical_path "$output_root")
if [ -e "$output_root" ] || [ -L "$output_argument" ] || [ -L "$output_root" ]; then
  echo "refusing to overwrite an existing run directory: $output_root" >&2
  exit 2
fi
mkdir "$output_root"
mkdir -p "$output_root/dxmt-logs"
if ! mkdir "$prefix_path"; then
  echo "could not reserve the fresh Wine prefix: $prefix_path" >&2
  exit 2
fi
cp "$app_source" "$output_root/d3d11_clear_window.exe"
app_path="$output_root/d3d11_clear_window.exe"
app_hash=$(shasum -a 256 "$app_path" | awk '{print $1}')
if [ "$lifecycle_test" = 1 ]; then
  skip_resize_test=0
else
  skip_resize_test=1
fi
printf 'engine=%s\nprefix=%s\napp=%s\napp_sha256=%s\nseconds=%s\ngeneration=%s\nstatic_texture=%s\nlifecycle_test=%s\nskip_resize_test=%s\nfailure_injection=%s\n' \
  "$engine_root" "$prefix_path" "$app_path" "$app_hash" "$seconds" \
  "$generation" "$static_texture" "$lifecycle_test" "$skip_resize_test" \
  "$fail_stage" > "$output_root/run-manifest.txt"

export WINEPREFIX="$prefix_path"
export WINEARCH=win64
export WINELOADER="$wine_path"
export WINESERVER="$wineserver_path"
export PATH="$engine_root/bin:$PATH"
export WINEDLLOVERRIDES='d3d10core=b;d3d11=b;dxgi=b;winemetal=b'
export WINEDEBUG=${WINEDEBUG:-+loaddll}
export DXMT_LOG_LEVEL=info
export DXMT_LOG_PATH="$output_root/dxmt-logs"
export DXMT_FRAMEGEN_ENABLE=1
export DXMT_FRAMEGEN_LOG="$output_root/native.csv"
export DXMT_FRAMEGEN_GENERATION="$generation"
export DXMT_FRAMEGEN_RUN_SECONDS="$seconds"
export DXMT_FRAMEGEN_STATIC_TEXTURE="$static_texture"
export DXMT_FRAMEGEN_SKIP_RESIZE="$skip_resize_test"
export DXMT_FRAMEGEN_LIFECYCLE_TEST="$lifecycle_test"
export DXMT_FRAMEGEN_TOGGLE_TEST="$lifecycle_test"
export DXMT_FRAMEGEN_FAIL=
if [ "$fail_stage" != none ]; then
  export DXMT_FRAMEGEN_FAIL="$fail_stage"
fi

set +e
"$wine_path" "$app_path" > "$output_root/wine-console.log" 2>&1
app_status=$?
set -e
printf 'app_exit=%s\n' "$app_status" > "$output_root/app-status.txt"

if [ -f "$output_root/native.csv" ] &&
   [ -f "$output_root/d3d11_clear_window_app.csv" ]; then
  python3 "$repo_root/tools/dxmt/analyze_framegen_run.py" \
    "$output_root/native.csv" "$output_root/d3d11_clear_window_app.csv" \
    --output "$output_root/summary.txt"
  ledger_status=$(awk '/^callback_tick_ledger: / { for (i = 1; i <= NF; ++i) if ($i ~ /^status=/) { sub(/^status=/, "", $i); print $i; exit } }' "$output_root/summary.txt")
  if [ "$ledger_status" != PASS ]; then
    echo "callback tick ledger is not verified: ${ledger_status:-missing}" >&2
    exit 1
  fi
else
  printf 'UNVERIFIED: application or native log was not produced; app_exit=%s\n' \
    "$app_status" > "$output_root/summary.txt"
  echo "application and native logs are both required for analysis" >&2
  exit 1
fi
exit "$app_status"
