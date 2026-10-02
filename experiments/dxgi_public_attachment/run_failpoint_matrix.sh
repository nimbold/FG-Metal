#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_BIN="${FG_STEP8D1_APP_BIN:-/tmp/step8d1_progression.exe}"
PROXY_BIN="${FG_STEP8D1_PROXY_BIN:-/tmp/step8d1_dxgi_proxy_gtest.dll}"
RUNNER="$SCRIPT_DIR/run_highball.sh"
EVIDENCE_DIR="${FG_STEP8D1_FAIL_EVIDENCE_DIR:-$SCRIPT_DIR/evidence/step8d1-failpoints}"
PREFIX="${FG_STEP8D1_PREFIX:-$HOME/Library/Caches/FG-Metal-Step8D-20261002/transparent-prefix}"

if [[ ! -f "$PROXY_BIN" ]]; then
    printf 'missing controlled-test proxy: %s\nbuild it with FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G=1; see README.md\n' \
        "$PROXY_BIN" >&2
    exit 2
fi
if [[ ! -f "$APP_BIN" ]]; then
    printf 'missing progression test app: %s\n' "$APP_BIN" >&2
    exit 2
fi

mkdir -p "$EVIDENCE_DIR"
: > "$EVIDENCE_DIR/results.txt"
{
    printf 'date_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'app_sha256=%s\n' "$(shasum -a 256 "$APP_BIN" | awk '{print $1}')"
    printf 'proxy_sha256=%s\n' "$(shasum -a 256 "$PROXY_BIN" | awk '{print $1}')"
    printf 'engine=%s\n' "${FG_STEP8D_ENGINE_ROOT:-$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r14}"
    printf 'prefix=%s\n' "$PREFIX"
} > "$EVIDENCE_DIR/run-manifest.txt"

failures=0
while IFS=' ' read -r point path; do
    [[ -z "$point" ]] && continue
    run_dir="$(mktemp -d "${TMPDIR:-/tmp}/fg-step8d1-fail-${point}.XXXXXX")"
    cp "$APP_BIN" "$run_dir/progression.exe"
    cp "$PROXY_BIN" "$run_dir/dxgi.dll"
    copy_enabled=1
    synthetic_enabled=0
    test_case=A
    if [[ "$path" == synthetic ]]; then
        copy_enabled=0
        synthetic_enabled=1
    fi
    (
        cd "$run_dir"
        FG_BOOKKEEPING_CASE="$test_case" \
        FG_TEST_FRAMES=15 \
        FG_TEST_FPS=0 \
        FG_DXGI_LOG=1 \
        FG_DXGI_COPY="$copy_enabled" \
        FG_DXGI_G="$synthetic_enabled" \
        FG_DXGI_TEST_FAILPOINT="$point" \
        FG_STEP8D_WINEDEBUG=-all \
        FG_STEP8D_PREFIX="$PREFIX" \
            bash "$RUNNER" ./progression.exe > highball.stdout 2>&1
    )
    app_status=$?
    [[ -f "$run_dir/progression_cases_app.log" ]] && cp "$run_dir/progression_cases_app.log" "$EVIDENCE_DIR/$point-app.log"
    [[ -f "$run_dir/fg_dxgi_proxy.log" ]] && cp "$run_dir/fg_dxgi_proxy.log" "$EVIDENCE_DIR/$point-proxy.log"
    cp "$run_dir/highball.stdout" "$EVIDENCE_DIR/$point-highball.stdout"
    app_result="missing"
    if [[ -f "$run_dir/progression_cases_app.log" ]]; then
        app_result="$(awk -F'result=' '/bookkeeping_result/{print $2}' "$run_dir/progression_cases_app.log" | tail -n 1 | tr -d '\r')"
    fi
    source_presents=0
    if [[ -f "$run_dir/progression_cases_app.log" ]]; then
        source_presents="$(awk '/^present frame=/{count++} END{print count+0}' "$run_dir/progression_cases_app.log")"
    fi
    failpoint_seen=0
    if rg -q 'result=disabled|result=injected_|result=failed' "$EVIDENCE_DIR/$point-proxy.log" 2>/dev/null; then
        failpoint_seen=1
    fi
    printf '%s path=%s app_exit=%s bookkeeping=%s source_presents=%s failpoint_log=%s\n' \
        "$point" "$path" "$app_status" "$app_result" "$source_presents" "$failpoint_seen" \
        | tee -a "$EVIDENCE_DIR/results.txt"
    if [[ "$app_status" -ne 0 || "$app_result" != PASS || "$source_presents" -ne 15 || \
          "$failpoint_seen" -ne 1 ]]; then
        failures=$((failures + 1))
    fi
done <<'EOF'
fence_create copy
event_create copy
allocator_create copy
commandlist_create copy
getbuffer copy
resource_create copy
allocator_reset copy
commandlist_reset copy
commandlist_close copy
queue_signal copy
fence_event copy
fence_timeout copy
descriptor_heap synthetic
synthetic_present synthetic
EOF

if [[ "$failures" -ne 0 ]]; then
    printf 'failure matrix failed %s expectation(s)\n' "$failures" >&2
    exit 1
fi
