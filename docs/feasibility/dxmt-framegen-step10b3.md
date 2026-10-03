# Step 10B.3 — single DXMT presentation owner and mandatory source escrow

**Result: PARTIAL PASS.** DXMT can keep one presentation owner, transfer sources only after their same-command-buffer escrow copy is submitted, and return to ordinary DXMT presentation through a quiescent handoff. Source-only operation, the optional-generation failure matrix, lifecycle changes, and a fresh 60-second run preserved every accepted source in the exercised cases. Positive display feedback remains below 60 Hz and does not bracket every displayed generated frame, so cadence and some A/G/B feedback remain incomplete.

**Recommendation:** continue the local DXMT route for cadence and feedback work. This result does not implement RIFE, alter Highball, or prepare an upstream DXMT change.

## Scope and artifacts

The downstream patch targets DXMT commit `fb4515681daefb789a4d0f403c4bdbca88f3b3de`. The test remains D3D11, SDR, single-sample, one controlled application, approximately 30 Presents/s, and a 60 Hz display link. The patch is local and downstream.

- [Final downstream patch](../../experiments/dxmt_framegen/patches/step10b3-dxmt-downstream.patch) — SHA-256 `cf45abb86fa51e3f581c2e3a9a8def700a82b72b6fdbe052dfde18fb60d95729`; applies cleanly to the pristine pinned checkout.
- [Source-only 30-second run](../../experiments/dxmt_framegen/evidence/step10b3/source-only-30s-repeat-clean/summary.txt)
- [Optional generation circuit-breaker run](../../experiments/dxmt_framegen/evidence/step10b3/failure-circuit-breaker-source-only/summary.txt)
- [Failure matrix evidence](../../experiments/dxmt_framegen/evidence/step10b3/failure-matrix/)
- [Fresh 60-second acceptance run](../../experiments/dxmt_framegen/evidence/step10b3/acceptance-60s-final/summary.txt)
- [Combined lifecycle run](../../experiments/dxmt_framegen/evidence/step10b3/lifecycle-65s-logfix/summary.txt)
- [Final 15-minute soak summary](../../experiments/dxmt_framegen/evidence/step10b3/soak-15m/summary.txt)
- [Final 15-minute native CSV (lossless gzip)](../../experiments/dxmt_framegen/evidence/step10b3/soak-15m/native.csv.gz)

The native CSV expands to 165 MB; it is gzip-compressed losslessly so the
evidence remains within GitHub's per-file limit. `analyze_framegen_run.py`
accepts the `.csv.gz` file directly.
- [Analyzer](../../tools/dxmt/analyze_framegen_run.py) and [analyzer tests](../../tests/test_analyze_framegen_run.py)

The downstream D3D11/WineMetal targets cross-build successfully. `git diff --check` passes in both the main repository and downstream checkout. The analyzer unit suite passes all 9 tests. No RIFE code, Highball file, or upstream DXMT checkout was changed.

## Presentation-owner state machine

| State | Layer owner | Behavior |
|---|---|---|
| `NORMAL` | Ordinary DXMT Presenter | `nextDrawable()` is allowed. Framegen has no active display-link owner. |
| `STARTING` | Ordinary DXMT Presenter until claim | The worker is prepared. DXMT drains its ordinary queue before enabling the display link. |
| `ACTIVE` | CAMetalDisplayLink and the Framegen worker | Every owned source has a mandatory escrow reservation. The worker presents SourceEscrow or optional G. Ordinary `nextDrawable()` is forbidden. |
| `DRAINING` | Existing Framegen owner until release | New sources stop entering the session; callbacks, accepted sources, presentation work, and synthetic work retire. |
| `BYPASS` | CAMetalDisplayLink and the Framegen worker | Optional G is disabled. The same worker continues presenting real SourceEscrow sources. |
| `FAILED` | The last verified owner remains authoritative | Ordinary presentation stays barred while Framegen still owns the layer. A failed quiesce does not authorize a competing drawable request. |

### NORMAL → ACTIVE

The first source primes the session through the ordinary Presenter. DXMT commits that source before calling `WaitUntilGPUIdle`. It then ensures the worker is running and calls `setEnabled(true)`. The native session claims the CAMetalLayer, attaches CAMetalDisplayLink, enables callbacks, and reports `session_active`. Only future accepted sources transfer to the worker.

### ACTIVE/BYPASS → NORMAL

When the request is disabled or the source becomes unsupported/occluded, `Present1` releases renderer and presentation locks before starting the transition. It marks the session draining, waits for accepted work to reach terminal states, pauses and invalidates the display link, retires callback drawables, stops and joins the worker, drains the GPU work, and releases the layer claim. The ordinary Presenter resumes only after `ownsLayer()` is false. The fullscreen test exposed a reentrant `ResizeBuffers` callback while the presentation lock was held; changing that lock to recursive resolved the hang, and fullscreen entry/exit then completed.

### Active-owner failure path

If source escrow setup fails after Framegen owns the layer, the reservation is cancelled. The queued DXMT encoding path quiesces Framegen before allowing the ordinary Presenter to acquire a drawable. If the transition cannot quiesce, the code keeps the active claim and refuses the ordinary drawable path. Runtime tests below cover successful quiescence; a forced quiesce timeout is not tested.

The diagnostic `_MetalLayer_nextDrawable` guard reports an ownership assertion if the ordinary path is reached under an active Framegen claim. None of the completed runtime logs contains that assertion. Normal-path `ordinary_source_present_submitted` records are the priming sources; fallback cases appear only after the native owner has been released.

## SourceEscrow and ownership transfer

Each source reservation allocates one private Metal texture in the source format. The same texture is stored as both the presentable source and generation history, so no optional second history copy is needed. The source texture remains strongly held until the required copy completes.

The DXMT encoding path encodes the blit after the application render commands, in the same Metal command buffer. It also registers completion tracking before commit. Preflight and copy-encoding failures cancel the reservation before the source can transfer. The post-commit task only records authority after the containing command buffer has been committed; it does not schedule a later source copy.

The exact `source_ownership_transfer` point is `_DXMTFramegenCommitSource`, called by the DXMT post-commit task. It records source ID, epoch, escrow texture ID, source command-buffer ID, and submit time only after checking that the record is prepared, committed, not cancelled, and still belongs to the active owner. The source copy completion is logged separately as `source_escrow_complete`.

On escrow allocation, preflight, or encoding failure, the source is not counted as Framegen-owned. It remains on ordinary DXMT presentation after the active owner is quiesced. Across injected failures, all app Presents remained successful; the preflight cancellation-fix run recorded exactly 151 cancelled reservations for 151 prepared candidates, with no duplicate cancellation.

If the shared source-render/escrow command buffer fails after commit, DXMT cannot prove which pixels were produced. That is a renderer/device failure, not an optional G failure. Metal does not expose a way in this test to inject escrow-only command-buffer failure while guaranteeing that the game render succeeded.

## Verified test results

### Source-only ACTIVE mode — 30 seconds

With generation disabled, the app made 900 Presents over 29.951 seconds (30.050 Hz). Framegen accepted 899 owned sources after the ordinary prime; all 899 reached safe terminal records, including 18 submitted-but-unconfirmed records. There were no lost accepted sources, no source regressions, and no ownership assertion. Positive display feedback measured 56.926 Hz. No G was displayed.

### Optional-generation failure and escrow setup matrix

All matrix runs were short, approximately 5 seconds each. App Presents returned success, and no ownership assertion or lost accepted source was recorded.

| Injected condition | Observed result |
|---|---|
| Escrow allocation failure | 151 app Presents stayed on ordinary DXMT; zero Framegen source acceptances. |
| Escrow preflight failure | 151 reservations cancelled before transfer; the layer was quiesced before ordinary presentation. |
| Escrow encode failure | Same pre-transfer fallback behavior; no Framegen-owned source was created. |
| Generator submission failure | 150 owned sources; all safe terminals; optional circuit opened and source presentation continued. |
| Generated allocation failure | 150 owned sources; all safe terminals; generated frames dropped without source loss. |
| Generated completion failure | 150 owned sources; all safe terminals. The injection simulates a failed generated result; it does not force an actual Metal device failure. |
| Synthetic generation failure | 150 owned sources; all safe terminals; source presentation continued. |
| Generated deadline miss | 150 owned sources; all safe terminals; 150 generated frames dropped. Positive display feedback fell to 32.136 Hz in this injected short run. |

The circuit-breaker run lasted 9.979 seconds: 300 accepted sources, 300 safe terminal outcomes, zero missing/lost IDs, one circuit-open event, and 57.255 Hz positive display feedback. It stayed on the worker's real-source presentation path. The display link remained in service until orderly shutdown.

The matrix did not isolate a genuine device-wide failure in the shared source-render/escrow command buffer. A completed generated buffer reported as failed by the injection is not evidence of real GPU failure.

### Fresh 60-second acceptance

The final-build run lasted 59.977 seconds. The app made 1,801 `Present` calls at 30.028 Hz; all returned `S_OK`. DXMT accepted 1,800 Framegen-owned sources and completed 1,801 escrow copies including the ordinary prime. Every accepted source had a safe terminal outcome: 1,613 had positive display feedback and 187 were submitted but remained unconfirmed because `presentedTime` was zero. There were zero missing, lost, regressed, duplicate, or unsafe accepted source IDs.

All 1,800 ownership-transfer rows had nonzero escrow texture and command-buffer IDs. There was one ordinary prime presentation, zero orphaned presentation submissions, and no ownership assertion. The native log recorded two claim/release cycles and drained callbacks, worker drawables, and synthetic GPU work on both.

The run requested 1,799 generated frames, dropped 45, and received positive display feedback for 1,755 G frames. The analyzer found 1,392 immediate A/G/B triples (79.3% of positively displayed G), with no malformed adjacent pair or source regression. The other 363 G records lacked one or both positively timestamped endpoints; 185 of those missing endpoints correlate with zero source feedback. Separately, 376 positive G feedback records arrived after B's scheduled target. Their physical display order is **unverified**, not evidence of a source loss. Positive display feedback measured 56.248 Hz (interval p50 16.666 ms, p95 45.441 ms, p99 46.159 ms, maximum 78.311 ms).

No ordinary-owner assertion appeared. The native event stream recorded two owner claims and releases: initial activation and final drain. `presentedTime == 0` never caused an accepted real source to be resubmitted; it remained `DisplaySubmittedUnconfirmed` and safe, while G could be dropped.

### Lifecycle and teardown — 65 seconds

The app completed 1,951 Presents over 64.977 seconds at 30.026 Hz, with zero failed HRESULTs. Thirty fullscreen/focus-related Presents returned non-failing occlusion statuses. Harness readback confirmed disable (`0`) and re-enable (`1`). Resize to 800×450, focus loss and recovery, fullscreen entry and exit, swapchain recreation, and process exit all succeeded.

The native event log retained all session instances. It recorded 8 layer claims/releases, 8 callback stop events, 8 worker-drawable retirement events, and 8 synthetic-GPU drain events. There were 1,722 accepted sources, all with safe terminal outcomes; zero accepted source was lost or unsafe. Seventy-four terminal sources had no positive `presentedTime`. Two history-capacity failures cancelled before ownership transfer and fell back after quiescence. Four generated submissions failed; source ownership remained safe and the test exited normally.

The run exercised shutdown with active presentation, accepted sources, generated work, GPU completions, and display callbacks in flight. It did not inject destruction separately at each requested internal state. Runtime evidence establishes the observed drain path, not every possible GPU/device-stall interleaving.

## `presentedTime`, DXGI, and evidence boundaries

The worker commits presentation work and calls `drawable.present()` before advancing the source submission frontier. Positive `presentedTime` is asynchronous visibility feedback. Zero feedback creates an unconfirmed submitted terminal; it does not resubmit the same source. G can be retired or dropped more aggressively. Tick and drawable record IDs, source/pair IDs, submit time, GPU completion, and feedback timestamps are logged where available.

The controlled application's DXGI `Present` results remained successful in the source-only, failure, lifecycle, and 60-second runs. This establishes the tested call results only. It does not measure physical panel scanout. GPU status for zero-`presentedTime` events is unavailable in the current records.

**VERIFIED:** same-command-buffer source escrow, post-commit ownership transfer, tested pre-transfer fallback, single-owner runtime guard, source-only active presentation, exercised optional failures, safe source terminals, app `S_OK` progression, lifecycle handoffs, and normal teardown drain.

**INFERRED:** an error of the shared source-render/escrow command buffer is unrecoverable because the renderer cannot know that the game's source pixels were successfully produced. This follows the shared command-buffer design and Metal failure semantics; no isolated copy-only failure was observed.

**NOT TESTED:** a real device-wide Metal failure; escrow-only GPU failure while game rendering succeeds; a forced bounded-quiesce timeout; worker exception/shutdown injection against an active owner; destruction separately at every pending copy/G/drawable state; and public physical scanout. The 363 unbracketed positive G feedback records in the 60-second run remain unverified as full visible A/G/B triples.

## Independent review

Seven requested GPT-6 Luna review areas were run against the evolving downstream candidate. Their source-only concerns were checked against the final tree and runtime evidence; they are not substitutes for runtime tests.

| Review area | Final assessment |
|---|---|
| Metal command-buffer/source-copy ordering | The final code encodes SourceEscrow in the app source command buffer and records transfer only in its post-commit task. A true shared command-buffer/device failure is not isolated from the game render. |
| Single presentation owner | The native layer claim is acquired before display-link attachment; the guard and active-owner failure path keep ordinary `nextDrawable()` barred until quiescence. Runtime assertion count is zero. |
| DXGI Present semantics | Controlled `Present` calls remained successful. Asynchronous renderer/device failure cannot be retroactively returned from a `Present` call that already returned; the tested setup failures quiesced before ordinary presentation. |
| Display-link lifecycle | Callback stop, owner release/reclaim, disable/re-enable, resize, focus, fullscreen, and swapchain recreation were exercised. The fullscreen reentrancy deadlock was fixed. |
| Resource lifetime | Source and escrow copies retain strong Metal references; history and presentation use the same immutable escrow. Stop cancels uncommitted reservations, and observed shutdown drained callbacks and GPU work. Individual pending-state destruction injections remain untested. |
| Failure classification | Optional G errors and deadlines preserve accepted sources; pre-transfer escrow setup failures remain ordinary DXMT presents. Real device-wide command-buffer failure was not injected. |
| Independent adversarial review | Current source was rechecked for worker restart and stop/commit cancellation. A bounded quiesce timeout under active-owner escrow failure remains untested and is a residual risk. |

- The final implementation now has a same-command-buffer escrow preparation path and exact post-commit ownership transfer. Early review notes describing the earlier separate-copy implementation were stale by the final candidate.
- The fullscreen deadlock concern was reproduced and corrected by using a recursive presentation mutex for the synchronous `WM_SIZE` → `ResizeBuffers` re-entry. The final lifecycle run completes fullscreen entry and exit.
- Worker restart is attempted from the active-owner path; a worker-exception injection was not run.
- Uncommitted stop reservations are marked cancelled before terminalization, and the commit callback rejects cancelled records. A deliberately raced stop/commit test was not run.
- A reviewer identified a residual edge: if mandatory escrow setup fails while the owner is active and quiescence itself times out, the ordinary drawable path remains barred after asynchronous encoding has begun. That failure is not injected here; the tested setup failures all quiesced and fell back safely. Treat this and a true device stall as unverified renderer/device failure behavior.

## Soak

The final-build soak completed 899.983 seconds (15 minutes) with the controlled application making 27,001 successful Presents at 30.002 Hz. The process exited with status 0; there were no failed HRESULTs, nonzero success/occlusion statuses, or buffer-query errors.

The native trace recorded 27,000 Framegen ownership transfers. Every transfer had a nonzero escrow texture ID and command-buffer ID. It recorded 27,001 escrow completions because the initial ordinary DXMT prime also completed an escrow copy. All 27,000 accepted Framegen sources had exactly one safe terminal outcome: zero accepted IDs were missing, duplicated, lost, or unsafe. There were 26,388 positively confirmed sources and 612 submitted-but-unconfirmed sources with zero `presentedTime`; none was resubmitted. The analyzer's single `orphan_terminal_ids=1` is source ID 1, the ordinary prime with `ordinary_fallback` detail, before Framegen accepted any source. It is not an unaccounted Framegen-owned source.

The layer had two claim/release cycles. Both stopped callbacks, retired worker drawables, drained generated submissions and presentation feedback, and drained synthetic GPU work. The initial ordinary prime was the only ordinary source presentation. There were zero presentation submissions without an owner, zero source-safety violations, zero circuit-open events, and no `_MetalLayer_nextDrawable` ownership assertion in the app or Wine logs. Two optional generator submissions returned errors; both were logged as dropped G work, the source path continued, and neither opened the circuit.

The worker logged 26,997 generated requests, 24,749 positively displayed G frames, and 2,252 generated drops. Of the displayed G frames, 23,534 had immediate positive A/G/B feedback brackets; 1,215 lacked at least one positively timestamped source endpoint. The run had no malformed generated pairs or source regressions. Positive display feedback averaged 56.831 Hz (interval p50 16.666 ms, p95 45.173 ms, p99 46.020 ms, maximum 97.493 ms), so the soak confirms sustained source safety and lifecycle drain but does not close the cadence or feedback gap. Physical panel scanout remains unverified.

## Final decision

**PARTIAL PASS.** The ownership and escrow architecture passes the exercised safety gates, and the source-only and failure paths continue to present the real source without competing `nextDrawable()` ownership. Lifecycle handoffs, the 60-second acceptance run, and the 15-minute soak preserved all accepted sources and drained cleanly. Cadence averaged 56.248 Hz in the acceptance run and 56.831 Hz in the soak; positive feedback did not certify a complete A/G/B bracket for every G. Continue the local DXMT route to resolve cadence and feedback; keep RIFE deferred until a follow-up demonstrates the missing brackets and validates the untested renderer/device failure boundaries.
