# Failed `presentDrawable:atTime:` attempt

The initial x86_64 one-shot host passed `update.targetPresentationTimestamp` to
`[commandBuffer presentDrawable:drawable atTime:target]`. Two runs crashed in
Metal's completion queue. The crash reports are retained on the host at:

- `/Users/<user>/Library/Logs/DiagnosticReports/framegen-dxmt-synthetic-metal-2026-10-03-015350.ips`
- `/Users/<user>/Library/Logs/DiagnosticReports/framegen-dxmt-synthetic-metal-2026-10-03-015402.ips`

Both triggered stacks include `-[CAMetalDrawable presentWithOptions:]` followed
by `__45-[_MTLCommandBuffer presentDrawable:options:]_block_invoke`. The reports
classify the fault as `EXC_BAD_ACCESS` / `SIGSEGV` with a possible pointer
authentication failure. The underlying Metal/translation-layer cause was not
identified. A separate first-run assertion about encoding an event wait after
opening the compute encoder was corrected by moving the wait before encoder
creation.

The host now uses the drawable associated with each display-link update and
calls `[commandBuffer presentDrawable:update.drawable]` with no `atTime:` and no
sleep pacing. The native arm64 bounded run completed with this path. The
x86_64 host stalled before its first architecture log line, so its display-link
path remains unverified. `MTLCreateSystemDefaultDevice()` runs before that line
and is a plausible location, but no stack trace attributes the stall to that
call.
