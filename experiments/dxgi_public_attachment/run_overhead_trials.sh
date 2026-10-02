#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_BIN="${FG_STEP8D1_APP_BIN:-/tmp/step8d1_progression.exe}"
PROXY_BIN="${FG_STEP8D1_PROXY_BIN:-/tmp/step8d1_dxgi_proxy.dll}"
RUNNER="$SCRIPT_DIR/run_highball.sh"
EVIDENCE_DIR="${FG_STEP8D1_OVERHEAD_EVIDENCE_DIR:-$SCRIPT_DIR/evidence/step8d1-overhead}"
PREFIX="${FG_STEP8D1_PREFIX:-$HOME/Library/Caches/FG-Metal-Step8D-20261002/transparent-prefix}"
FRAMES="${FG_STEP8D1_OVERHEAD_FRAMES:-2000}"

mkdir -p "$EVIDENCE_DIR"
{
    printf 'date_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'app_sha256=%s\n' "$(shasum -a 256 "$APP_BIN" | awk '{print $1}')"
    printf 'proxy_sha256=%s\n' "$(shasum -a 256 "$PROXY_BIN" | awk '{print $1}')"
    printf 'frames_per_trial=%s\n' "$FRAMES"
    printf 'engine=%s\n' "${FG_STEP8D_ENGINE_ROOT:-$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r14}"
    printf 'prefix=%s\n' "$PREFIX"
} > "$EVIDENCE_DIR/run-manifest.txt"
printf 'scenario,trial,exit,frames,elapsed_ticks,qpc_frequency,source_fps,present_median_ticks,present_p95_ticks,working_set_bytes,private_bytes\n' > "$EVIDENCE_DIR/summary.csv"

for scenario in baseline passthrough; do
    for trial in 1 2 3; do
        run_dir="$(mktemp -d "${TMPDIR:-/tmp}/fg-step8d1-overhead-${scenario}-${trial}.XXXXXX")"
        cp "$APP_BIN" "$run_dir/progression.exe"
        if [[ "$scenario" == passthrough ]]; then cp "$PROXY_BIN" "$run_dir/dxgi.dll"; fi
        (
            cd "$run_dir"
            FG_BOOKKEEPING_CASE=A \
            FG_TEST_FRAMES="$FRAMES" \
            FG_TEST_FPS=0 \
            FG_DXGI_LOG=0 \
            FG_DXGI_COPY=0 \
            FG_DXGI_G=0 \
            FG_STEP8D_WINEDEBUG=-all \
            FG_STEP8D_PREFIX="$PREFIX" \
                bash "$RUNNER" ./progression.exe > highball.stdout 2>&1
        )
        app_status=$?
        cp "$run_dir/progression_cases_app.log" "$EVIDENCE_DIR/$scenario-$trial-app.log"
        cp "$run_dir/highball.stdout" "$EVIDENCE_DIR/$scenario-$trial-highball.stdout"
        awk -v scenario="$scenario" -v trial="$trial" -v app_status="$app_status" \
            -v frames="$FRAMES" '
            /^timing qpc_frequency=/ { split($2, a, "="); freq=a[2] }
            /^complete frames=/ { split($2, a, "="); actual=a[2]; split($3, b, "="); elapsed=b[2] }
            /^present frame=/ { for (i=1; i<=NF; ++i) if ($i ~ /^duration_ticks=/) { split($i, d, "="); durations[++n]=d[2] } }
            /^memory_sample / { for (i=1; i<=NF; ++i) { if ($i ~ /^current_working_set=/) { split($i, m, "="); ws=m[2] } if ($i ~ /^current_private=/) { split($i, m, "="); priv=m[2] } } }
            END {
                for (i=1; i<=n; ++i) for (j=i+1; j<=n; ++j) if (durations[i] > durations[j]) { t=durations[i]; durations[i]=durations[j]; durations[j]=t }
                median=durations[int((n+1)*0.50)]; p95=durations[int((n+1)*0.95)];
                fps=(elapsed > 0 ? actual*freq/elapsed : 0)
                printf "%s,%s,%s,%s,%s,%s,%.4f,%s,%s,%s,%s\n", scenario, trial, app_status, actual, elapsed, freq, fps, median, p95, ws, priv
            }
        ' "$run_dir/progression_cases_app.log" >> "$EVIDENCE_DIR/summary.csv"
        printf '%s trial=%s app_exit=%s\n' "$scenario" "$trial" "$app_status"
    done
done
