#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_BIN="${FG_STEP8D1_APP_BIN:-/tmp/step8d1_progression.exe}"
PROXY_BIN="${FG_STEP8D1_PROXY_BIN:-/tmp/step8d1_dxgi_proxy_gtest.dll}"
RUNNER="$SCRIPT_DIR/run_highball.sh"
EVIDENCE_DIR="${FG_STEP8D1_EVIDENCE_DIR:-$SCRIPT_DIR/evidence/step8d1-matrix}"
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
    printf 'frames=15\nfps=uncapped\n'
} > "$EVIDENCE_DIR/run-manifest.txt"

failures=0
for scenario in baseline passthrough copy synthetic; do
    for test_case in A B C D E; do
        run_dir="$(mktemp -d "${TMPDIR:-/tmp}/fg-step8d1-${scenario}-${test_case}.XXXXXX")"
        cp "$APP_BIN" "$run_dir/progression.exe"
        if [[ "$scenario" != baseline ]]; then
            cp "$PROXY_BIN" "$run_dir/dxgi.dll"
        fi

        copy_enabled=0
        synthetic_enabled=0
        if [[ "$scenario" == copy ]]; then copy_enabled=1; fi
        if [[ "$scenario" == synthetic ]]; then synthetic_enabled=1; fi

        (
            cd "$run_dir"
            FG_BOOKKEEPING_CASE="$test_case" \
            FG_TEST_FRAMES=15 \
            FG_TEST_FPS=0 \
            FG_DXGI_LOG=1 \
            FG_DXGI_COPY="$copy_enabled" \
            FG_DXGI_G="$synthetic_enabled" \
            FG_STEP8D_WINEDEBUG=-all \
            FG_STEP8D_PREFIX="$PREFIX" \
                bash "$RUNNER" ./progression.exe > highball.stdout 2>&1
        )
        app_status=$?

        app_log="$run_dir/progression_cases_app.log"
        proxy_log="$run_dir/fg_dxgi_proxy.log"
        if [[ -f "$app_log" ]]; then
            cp "$app_log" "$EVIDENCE_DIR/${scenario}-${test_case}-app.log"
        fi
        if [[ -f "$proxy_log" ]]; then
            cp "$proxy_log" "$EVIDENCE_DIR/${scenario}-${test_case}-proxy.log"
        fi
        cp "$run_dir/highball.stdout" "$EVIDENCE_DIR/${scenario}-${test_case}-highball.stdout"

        app_result="missing"
        if [[ -f "$app_log" ]]; then
            app_result="$(awk -F'result=' '/bookkeeping_result/{print $2}' "$app_log" | tail -n 1 | tr -d '\r')"
        fi
        printf '%s %s app_exit=%s bookkeeping=%s\n' \
            "$scenario" "$test_case" "$app_status" "$app_result" \
            | tee -a "$EVIDENCE_DIR/results.txt"
        expected_status=0
        expected_result=PASS
        if [[ "$scenario" == synthetic && "$test_case" != A ]]; then
            expected_status=1
            expected_result=FAIL
        fi
        if [[ "$app_status" -ne "$expected_status" || "$app_result" != "$expected_result" ]]; then
            printf 'unexpected result: expected app_exit=%s bookkeeping=%s\n' \
                "$expected_status" "$expected_result" >&2
            failures=$((failures + 1))
        fi
    done
done

if [[ "$failures" -ne 0 ]]; then
    printf 'progression matrix failed %s expectation(s)\n' "$failures" >&2
    exit 1
fi
