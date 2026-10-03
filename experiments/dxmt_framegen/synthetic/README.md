# Isolated synthetic Metal/display harness

This downstream experiment implements `FrameGenerationBackend` through
`framegen::FrameGenerator` without changing Framegen's public API or core. It
imports native `id<MTLTexture>` objects, makes fixed GPU patterns A and B,
blends them into G with a Metal compute kernel at fraction 0.5, and feeds the
textures to a minimal AppKit/MTKView host driven by `CAMetalDisplayLink`.

The source tick targets 30 Hz and the display link requests 60 Hz. The host
presents the drawable supplied for each update with
`commandBuffer presentDrawable:update.drawable`. Blend readiness comes from a
Metal command-buffer completion callback and is passed to the scheduler using
its monotonic completion timestamp. Display work waits on shared Metal events;
the host never maps or reads pixels. CSV records callback and scheduler target
times, raw CAMetalDisplayLink target-presentation time, raw drawable
`presentedTime`, measured callback-to-target phase, source pair timestamps,
source GPU-ready time, generated-ready state/time, A/G/B selection, and drops.

For scheduler feedback only, the native `presentedTime` is translated backward
by the measured callback-to-target phase because this harness uses callback
time for scheduler choices. The raw Metal target and raw `presentedTime` remain
separate in CSV. This is a harness adapter detail; the scheduler target should
not be read as the actual visible time.

The host passes CAMetalDisplayLink's raw `targetTimestamp` through as the
render-submit deadline. A callback arriving after that timestamp is counted in
`expired_render_deadlines` and the scheduler sees the expired deadline. The
retained v1/v2/v3 captures predate this correction: those runs clamped the
deadline to at least callback time. Their scheduler deadline and selection
metrics do not verify behavior with raw expired deadlines. The post-correction
v4 run had zero expired callback deadlines, so the late-deadline branch remains
unexercised; it still provides current-source cadence and source-pair telemetry.

This is an isolated backend/display harness, not DXMT-presenter integration.
It does not validate DXMT texture import, Wine's presenter path, or a clean x86
Metal runtime.

## x86_64 build and launch

Run from the repository root on macOS 14 or later:

```sh
cmake -S experiments/dxmt_framegen/synthetic \
  -B build/step10b-synthetic-x86_64 -G Ninja \
  -DCMAKE_OSX_ARCHITECTURES=x86_64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
cmake --build build/step10b-synthetic-x86_64 \
  --target framegen-dxmt-synthetic-metal --verbose
arch -x86_64 \
  build/step10b-synthetic-x86_64/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal \
  --duration-seconds 60 \
  --output build/step10b-synthetic-x86_64/evidence
```

Each `--output` path must name a new directory; the host refuses to truncate
evidence from an earlier run. On shutdown it gives outstanding generator,
drawable, and command-buffer callbacks up to five seconds to retire. If the
drain times out or evidence cannot be written, the process exits nonzero and
the summary records the pending counts.

Both x86_64 configure/builds succeeded and `file` confirmed a Mach-O x86_64
binary. Runtime stalled before the first architecture line or any display
callbacks, with a zero-byte smoke log. The source calls
`MTLCreateSystemDefaultDevice()` before that first line, making device startup a
plausible location; no stack trace establishes that call as the cause.
See [the blocked-startup record](../evidence/step10b/x86_64-blocked/README.md)
and [the failed atTime attempt](../evidence/step10b/failed-at-time-attempt.md).

## Native arm64 diagnostic

The same source was configured and built as arm64, then run on Apple M3 to
distinguish the host flow from the Rosetta startup blocker. Captures are
retained under `experiments/dxmt_framegen/evidence/step10b/arm64-v1/` through
`arm64-v4/`, with each CSV, summary, and process log preserved.

The corrected-source v4 run observed 28.846 source updates/s and 58.560 display
callbacks/s for 61.014 seconds. It submitted 1,683 blends; all became GPU-ready
and 1,678 were selected as G. It recorded 863 A and 1,031 B selections, 3,569
timestamped presentations, three zero-`presentedTime` outcomes, 50 scheduler
deadline misses, and 81 dropped generated opportunities. No callback arrived
after its raw render deadline, so late-callback rejection was not exercised.
Native actual-minus-target latency was 68.834 μs at p50 and 16.713209 ms at p95;
G was presented 83.272 ms p50 after its source-pair midpoint. Review found 466
of 1,678 G presentations bracketed by successful presentations of the exact
source pair, but only 144 immediate A→G→B and 142 immediate B→G→A triplets
matched that same pair. Other output rows repeat or regress source IDs, so the
trace does not establish sustained source-aligned 2x cadence. The prior v3 run
predates the raw deadline correction and is not evidence for current deadline
handling.

The arm64 runs are diagnostic evidence only; they do not satisfy the x86_64
Wine target or validate DXMT integration. See the [retained run table and
limitations](../evidence/step10b/README.md).

## Output fields

`synthetic-display.csv` records callback, scheduler, native display target, raw
actual presentation, scheduler-normalized feedback, source-pair timestamps and
source GPU-ready time, generated-ready state/time, selection, disposition, and
drop reason. Missing native timing values are empty CSV fields, and free-form
drop reasons are CSV-escaped. Generated-ready state/time refers to the exact G
selected for that callback, not the most recently completed generation.
`synthetic-summary.txt` records observed source/display rates,
submissions, readiness, selection counts, pending callbacks, drops, and native
target latency percentiles.
