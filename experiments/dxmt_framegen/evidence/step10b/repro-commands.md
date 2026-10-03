# Step 10B reproducible commands

Run from the repository root on macOS 14 or later.

## x86_64 configure and build

```sh
cmake -S experiments/dxmt_framegen/synthetic \
  -B build/step10b-synthetic-x86_64 -G Ninja \
  -DCMAKE_OSX_ARCHITECTURES=x86_64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
cmake --build build/step10b-synthetic-x86_64 \
  --target framegen-dxmt-synthetic-metal --verbose
file build/step10b-synthetic-x86_64/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal
```

The x86_64 build and Mach-O architecture check succeeded. The runtime command
below was attempted once with a requested 2-second capture duration (it had no
process-level timeout). It stalled before
the first architecture line and produced the retained zero-byte log at
`x86_64-blocked/smoke.log`; it did not begin callbacks or present frames.
The source calls `MTLCreateSystemDefaultDevice()` before the first architecture
line, but no stack trace attributes the stall to that call.

```sh
arch -x86_64 \
  build/step10b-synthetic-x86_64/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal \
  --duration-seconds 2 \
  --output build/step10b-synthetic-x86_64/smoke-evidence \
  > build/step10b-synthetic-x86_64/smoke.log 2>&1
```

## Native arm64 diagnostic

The native runs used the same source and host flow, but do not satisfy the
x86_64 Wine requirement.

```sh
cmake -S experiments/dxmt_framegen/synthetic \
  -B build/step10b-synthetic-arm64 -G Ninja \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
cmake --build build/step10b-synthetic-arm64 \
  --target framegen-dxmt-synthetic-metal --verbose
arch -arm64 \
  build/step10b-synthetic-arm64/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal \
  --duration-seconds 60 \
  --output build/step10b-synthetic-arm64/evidence-v3 \
  > build/step10b-synthetic-arm64/run-v3.log 2>&1
```

The completed v3 capture is preserved in `arm64-v3/`. `arm64-v1/` and
`arm64-v2/` contain the earlier run evidence and their original process logs.

## Corrected-source arm64 diagnostic (v4)

This run used the raw `CAMetalDisplayLink` `targetTimestamp` as the scheduler's
render-submit deadline, without clamping it to callback time. Configure/build
logs and the 61-second CSV, summary, and runtime log are in `arm64-v4/`.

```sh
cmake -S experiments/dxmt_framegen/synthetic \
  -B /tmp/fg-step10b-synthetic-arm64-v4 -G Ninja \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
cmake --build /tmp/fg-step10b-synthetic-arm64-v4 \
  --target framegen-dxmt-synthetic-metal --verbose
arch -arm64 \
  /tmp/fg-step10b-synthetic-arm64-v4/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal \
  --duration-seconds 60 \
  --output experiments/dxmt_framegen/evidence/step10b/arm64-v4
```

The trace did not produce any expired callback deadlines, but it still had
scheduler misses, uneven native target offsets, and source-ID repeats/regressions.
It is an isolated diagnostic and does not establish DXMT integration or valid
A→G→B source ordering.
