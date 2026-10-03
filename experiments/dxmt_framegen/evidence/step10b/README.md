# Step 10B synthetic host evidence

This directory contains durable copies of the runtime evidence gathered under
ignored `build/`. The implementation is the isolated synthetic Framegen Metal
backend/display harness; none of these files demonstrate DXMT-presenter
integration.

## Captures

- `arm64-v1/`: first 60-second callback-polled readiness run. The raw summary
  reported 1,741 sources (28.280 Hz), 3,574 callbacks (58.054 Hz), 492 blend
  submissions, only one selected G, 493 scheduler misses, and 491 late blend
  drops. Readiness polling at display callbacks observed completion too late.
- `arm64-v2/`: second 60-second run with asynchronous Metal command-buffer
  completion callbacks. It observed 1,788 sources (29.725 Hz), 3,584 callbacks
  (59.583 Hz), 636 submissions/completions/G selections, A=1,433, B=1,514,
  3,580 presentations, and three native presentation drops. Scheduler feedback
  still used callback-time targets against raw later presentation times, which
  produced 635 deadline misses and reduced submitted G cadence. This run is
  retained as the failed phase-alignment attempt.
- `arm64-v3/`: final 60-second phase-normalized feedback run. It observed 1,757
  sources (28.823 Hz), 3,562 callbacks (58.433 Hz), 1,578 blend
  submissions/completions, 1,572 G selections, A=1,003, B=986, 3,557 actual
  presentations, and four native drops. The scheduler recorded 116 deadline
  misses and 184 dropped generated opportunities. Raw drawable actual times
  remain separate from scheduler-normalized feedback. Native actual-minus-target
  latency was 66,167 ns p50 and 16,768,792 ns p95.
- `arm64-v4/`: 61.014-second run from the corrected source after the scheduler
  was changed to receive the raw `targetTimestamp`. It observed 1,760 source
  updates (28.846 Hz), 3,573 callbacks (58.560 Hz), 1,683 submitted/ready
  blends, and 1,678 G selections; selection totals were A=863 and B=1,031.
  There were 3,569 timestamped presentations and three zero-`presentedTime`
  outcomes. The scheduler recorded 50 deadline misses and 81 dropped generated
  opportunities; the three presentation drops were two A and one B, with no G
  drop. Callback render deadlines were not expired. Native actual-minus-target
  latency was 68.834 μs p50 and 16.713209 ms p95; G actual presentation was
  83.272 ms p50 after its source-pair midpoint and 49.938 ms p50 after the
  callback-time scheduler target. Of 1,678 G presentations, 466 were bracketed
  by successful presentations of the exact source pair (227 A→G→B and 239
  B→G→A, not all immediate). Only 144 immediate A→G→B and 142 immediate
  B→G→A triplets matched the same adjacent source pair. Other selections repeat
  or regress source IDs (for example B2, G from pair 2→3, then B2), so this
  trace does not establish sustained source-aligned 2x cadence. It is standalone
  arm64 telemetry, not DXMT integration or visual pixel verification.
- `x86_64-blocked/`: zero-byte smoke log and process-state record. The x86_64
  binary builds, but its runtime stalled before the first architecture line;
  no CAMetalDisplayLink callback began. The source calls
  `MTLCreateSystemDefaultDevice()` before that line, so device startup is a
  plausible location, but no stack trace proves the cause.
- `failed-at-time-attempt.md`: crash locations and retained local crash-report
  paths from the prior `presentDrawable:atTime:` experiment.

`arm64-v1/run.log` confirms the first run actually executed as arm64; the raw
v1 summary contains a known hardcoded `process_arch=x86_64` label. The v2, v3,
and v4 summaries report arm64 correctly. The x86_64 target is the required Wine target;
arm64 traces only validate host/display sequencing on Apple M3. No capture
validates DXMT's presenter, WineMetal texture bridging, or x86_64 runtime.

Use [`repro-commands.md`](repro-commands.md) for exact build and launch commands.

The v1/v2/v3 traces were recorded before the host passed raw CAMetalDisplayLink
`targetTimestamp` through as the render-submit deadline. They used a callback-
time clamp and do not verify expired-deadline handling. The v4 run exercises
the corrected path; it had no expired callback deadline, and its source order
and presentation timing still fall short of the required validation.
