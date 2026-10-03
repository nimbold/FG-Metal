# Step 10B.2 — source ownership and DXMT display-tick recovery

**Result: FAIL.** The diagnostic branch now records explicit source states, retains a required presenter snapshot separately from optional generation history, and contains source-only/restart paths. It still transfers presentation ownership before a presentable snapshot is guaranteed, has no recovery for failure of that required snapshot, and two async-copy failure smokes abort in DXMT's ordinary `nextDrawable` path while `CAMetalDisplayLink` owns the layer. An extra bypass-detach guard also failed to remove the crash. No fresh 60-second acceptance run or soak was eligible.

## Scope and artifacts

**VERIFIED**

- The experimental patch is based on DXMT `fb4515681daefb789a4d0f403c4bdbca88f3b3de` in `/tmp/dxmt-fb451568-evidence.opkf8x/downstream`. The pristine checkout at `/tmp/dxmt-fb451568-evidence.opkf8x/repo` remains clean. This is a local diagnostic patch, not an upstream contribution.
- The downstream diff is 16 DXMT files and 3,579 patch lines. `git diff --check` passes; `git apply --check` passes against the pristine checkout. Patch SHA-256: `b2ea00f2cb58f4e7690ebb4e97de40f7a742c947106aef08dd36a4834bc09424`.
- DXMT D3D11 and WineMetal targets build successfully in `/tmp/dxmt-step10b1-wine-x64` (Ninja exit 0; targets were up to date apart from version generation). The 8 analyzer unit tests pass. These checks establish build and parser behavior only.
- The controlled test application uses D3D11 `Present1`; each failed smoke logged three `S_OK` presents, no query errors, and backbuffer index 0 before and after each call. The first app-log span was 34 ms and the later retry span was 14 ms; their apparent rates are startup sampling, not cadence results.
- The implementation uses Metal texture copies and GPU generation; source inspection found no CPU image readback, screen capture, private D3DMetal/Wine hooks, Highball changes, or RIFE integration.

Artifacts:

- [Cumulative local DXMT diagnostic patch](../../experiments/dxmt_framegen/patches/step10b2-dxmt-downstream.patch)
- [First post-lifecycle-fix analysis](../../experiments/dxmt_framegen/evidence/step10b2/crash-fix-copy-completion/analysis.txt)
- [First post-lifecycle-fix native log](../../experiments/dxmt_framegen/evidence/step10b2/crash-fix-copy-completion/native.csv)
- [First post-lifecycle-fix app log](../../experiments/dxmt_framegen/evidence/step10b2/crash-fix-copy-completion/d3d11_clear_window_app.csv)
- [First post-lifecycle-fix Wine log](../../experiments/dxmt_framegen/evidence/step10b2/crash-fix-copy-completion/wine-console.log)
- [Latest bypass-guard retry analysis](../../experiments/dxmt_framegen/evidence/step10b2/retry-detach-bypass/analysis.txt)
- [Latest bypass-guard retry native log](../../experiments/dxmt_framegen/evidence/step10b2/retry-detach-bypass/native.csv)
- [Latest bypass-guard retry app log](../../experiments/dxmt_framegen/evidence/step10b2/retry-detach-bypass/d3d11_clear_window_app.csv)
- [Latest bypass-guard retry Wine log](../../experiments/dxmt_framegen/evidence/step10b2/retry-detach-bypass/wine-console.log)
- [Earlier failed-run analysis](../../experiments/dxmt_framegen/evidence/step10b2/failure-copy-completion/analysis.txt)
- [Analyzer source](../../tools/dxmt/analyze_framegen_run.py)

The latest injected run used `DXMT_FRAMEGEN_FAIL=copy-completion`. Its app executable SHA-256 is `343178533e44c359ca0b67a2ae62cd91e97ba4282bbe5370c763385ae15d8f89`.

### Evidence labels

- **VERIFIED:** both optional-copy-failure smokes accepted two IDs and completed their required snapshots, then aborted with the same `nextDrawable` exception. The latest retry used the newly compiled unconditional bypass-detach guard. Both traces have three S_OK app Presents, no positive renderer presentations, and no source terminal outcomes.
- **INFERRED:** the exception is consistent with the ordinary DXMT Presenter reaching `nextDrawable` while CAMetalDisplayLink drawable ownership still applies. Neither trace has a branch-level marker identifying which DXMT call site reached `_MetalLayer_nextDrawable`; the exact route remains unresolved.
- **NOT TESTED:** required-snapshot async failure recovery; all other fresh failure injections; lifecycle/teardown stress; a fresh 60-second acceptance run; and the 15-minute soak.

## Source ownership state machine

The bridge's exact source states are `Accepted`, `SnapshotCopyPending`, `Owned`, `PresentPending`, `ReplayPending`, `Presented`, `Retired`, and `Failed`. Its terminal outcome enum is `RendererPresented`, `OrdinaryFallback`, `UnsafeLostSnapshot`, or `DestroyedBeforePresentation` (the last two are explicitly unsafe). The swapchain separately tracks `Stopped`, `Starting`, `Active`, `Bypass`, `Stopping`, and `Failed`. Generated pair records track readiness, selection, presentation, retirement, and failure separately from source state.

The intended real-source progression is:

```text
ACCEPTED → SNAPSHOT_COPY_PENDING → OWNED → PRESENT_PENDING
         → PRESENTED → RETIRED
                         ↘ REPLAY_PENDING → PRESENTED
                         ↘ FAILED
```

Pair generation is separately tracked as pending, ready, selected, presented, or dropped. The code logs transitions and terminal outcomes, and it classifies an unrecoverable required-snapshot completion as `UnsafeLostSnapshot`.

**BLOCKER — ownership transfer is still too early.** In `src/d3d11/d3d11_swapchain.cpp`, the application Presenter is suppressed when `ctx.afterCommit(...)` registration succeeds. The callback later calls `commitSource`, which queues the required snapshot copy. Thus the real transfer point is the successful scheduling of a callback, before the required snapshot is committed or completed. The native `source_accepted` event is emitted later and describes a stronger condition than the swapchain has actually guaranteed.

The snapshot-completion handler in `src/winemetal/unix/dxmt_framegen_bridge.mm` clears the retained original source. On a required-snapshot failure it records `UnsafeLostSnapshot` and opens the circuit; there is no exact-source replay resource. The source may already have been suppressed. No injected `snapshot-completion` run was performed, so the loss branch is source-inspection evidence, not a runtime count.

**Exact ownership-transfer point:** currently, the DXMT encode callback's successful `afterCommit` registration. This violates the requested contract. The required safe point would be after a presentable immutable source is committed in known-good GPU ordering, while an independent replay resource still survives any subsequent optional work failure.

## Async-copy failure and ordinary-source fallback

The only fresh async-copy injection targeted the optional generation-history copy. In the latest trace:

| Observation | Result |
|---|---:|
| App `Present1` calls / errors | 3 / 0 in each short smoke |
| Accepted source IDs | 1 and 2 |
| Required source snapshots completed | 2 |
| Injected optional history-copy failures | 2 |
| Circuit-open events | 2 |
| Positive renderer presentation events | 0 |
| Accepted IDs with a terminal source outcome | 0 of 2 |
| Generated presentation events | 0 |

The snapshots did survive the injected *optional* history failures. In the first run, source 1 was selected for replay; in the second, four display callbacks were recorded but no source was selected. Neither run positively presented a source or recorded a terminal outcome before abort. The log therefore does **not** prove replay success. It also does not test failure of the required presenter snapshot or a real Metal command-buffer error.

For the latest run specifically, the analyzer's `lost_source_ids=2` means both accepted IDs had no terminal record when the process aborted. It is not independent proof that the source pixels disappeared. **Confirmed source presentations: 0; unresolved accepted sources at abort: 2; confirmed content-loss count: undetermined.** The acceptance invariant still fails because neither source reached a safe terminal outcome.

Both Wine logs report `CAMetalLayerInvalidOperation: -nextDrawable should not be called when using CAMetalDisplayLink`, with the stack entering `winemetal.so`'s `_MetalLayer_nextDrawable`. This is a runtime failure while the ordinary DXMT Presenter requests a drawable. It is not a nil-drawable injection result. The attempted pause/invalidate/recreate change and a later unconditional bypass-detach guard did not eliminate this conflict in the tested runs.

**Fail-open result: FAIL.** Optional generation work is supposed to be expendable, but this run cannot demonstrate the same source reaching renderer presentation after that work fails. Required-snapshot failure has no replay path in the code.

## Presentation commit point and strict output order

The current source frontier advances only after both GPU completion and positive drawable `presentedTime`. A selection or worker dequeue is not treated as final, and zero-time feedback remains retryable. This avoids consuming a source at encode time, but it does not implement the requested “successful presentation submission” commit point. A zero-time result may be retried even after `present()` was submitted, which can duplicate a source; the retry/duplicate behavior was not exercised in this run.

Static inspection of the selector shows chronological source selection and exact adjacent pair keys for optional `G(A,B)`. It drops or invalidates generated work when the pair is stale, late, or no longer adjacent. The independent ordering audit found the static A/G/B constraints structurally sound. Runtime ordering remains **NOT TESTED** in Step 10B.2: the latest run produced zero generated presentations, zero exact triples, and zero positive source presentations. No pair-order correctness claim can be made from that run.

The previous Step 10B.1 run remains the only sustained ordering sample: 1,642 generated presentations, 1,601 exact immediate A→G(A,B)→B triples, 41 without both immediate positively timestamped endpoints, 17 repeated source presentations, and no reported source regressions. It remains a failed, superseded-by-this-work diagnostic, not Step 10B.2 acceptance evidence.

## `presentedTime == 0` and pacing tail

No zero-time outcomes occurred in the latest Step 10B.2 diagnostic because no renderer presentation reached feedback. The earlier Step 10B.1 run had 148 zero-time outcomes: 123 generated and 25 source. The retained records do not include enough linked evidence—particularly a drawable identity and reliable GPU status—to determine whether those events correlate with drawable lateness/replacement, worker scheduling, GPU completion, or another cause. The requested deeper correlation is **NOT TESTED**.

The previous 60-second timing distribution remains the reference, not a new result:

| Metric | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Positive display interval | 16.666 ms | 45.609 ms | 47.010 ms | 94.875 ms |
| Display callback | 10.833 µs | 24.209 µs | 41.125 µs | 1,117.041 µs |
| History-copy completion | 1.937 ms | 3.256 ms | 9.369 ms | 64.081 ms |
| Synthetic generation submit→ready | 15.819 ms | 17.059 ms | 19.091 ms | 70.158 ms |
| Presentation GPU submit→completion | 0.817 ms | 1.522 ms | 2.163 ms | 13.986 ms |

The B1 trace shows that callback duration was usually short, while the display interval had a large tail. It does not identify the cause. In the short B2 run, callback-to-worker-wake was 0.029 ms p50 / 7.477 ms p95 from two samples, and device-mutex wait was at most 0.001 ms from two samples. Presenter encode, GPU submit, `present()` call, and feedback timings were unavailable. These small and sparse samples cannot explain or clear the old tail.

## Restart, lifecycle, resize, and teardown

The patch adds explicit `STOPPED`, `STARTING`, `ACTIVE`, `BYPASS`, `STOPPING`, and `FAILED` swapchain states. Its bypass path pauses and invalidates the display link; re-enable creates a fresh link on the same layer and resets the epoch. A later guard now calls `setEnabled(false)` on every ordinary-present fallback even when the lifecycle enum already says `BYPASS`; it compiled but the runtime exception remained. Neither run verifies a successful bypass→enable cycle. Restartability is **NOT VERIFIED**.

No runtime resize, focus loss/recovery, fullscreen/windowed transition, swapchain recreation, disable→enable, or pending-work destruction case was run. The teardown matrix is therefore **NOT TESTED** for all requested states: no work; pending snapshot copy; owned source; pending generation; ready G; selected source; presentation GPU work; and callback in flight.

Static inspection found two more teardown hazards:

- `request_stop` can mark accepted, unpresented sources `DestroyedBeforePresentation`, which is not a safe source terminal outcome.
- A stop can race a queued `afterCommit` callback: teardown may retire the still-uncommitted reservation, then the callback attempts a disallowed transition. This can produce an assertion or inconsistent terminal record.

The synthetic backend also owns a separate Metal command queue. Teardown's DXMT immediate-queue idle wait does not by itself prove that this generator queue or its retained resources have drained. No use-after-free or hang was observed in a completed matrix because that matrix was not run; the process-abort smoke is not a teardown pass.

## Failure matrix and acceptance gates

| Case | Step 10B.2 status |
|---|---|
| Allocation failure | Not rerun |
| Copy preflight failure | Not rerun |
| Optional history async-completion failure | Injected twice; snapshot retained, but process aborted before source terminal outcome |
| Required snapshot async-completion failure | Not injected; source-inspection branch is unsafe |
| Generator submission / synthetic generation / generated completion failure | Not rerun |
| Nil or invalid drawable | Not injected; ordinary `nextDrawable` path crashed during copy-failure smoke |
| Expired G deadline | Not rerun |
| Worker shutdown / lifecycle transition with work pending | Not rerun |
| Resize / focus / fullscreen / recreation | Not tested |

The old Step 10B.1 short failure runs do not satisfy the request to repeat the matrix after the Step 10B.2 changes.

No fresh 60-second 30→60 run was started because the source-ownership and drawable-failure blockers remain. Consequently there are no Step 10B.2 source-loss, pair-order, cadence, or 60-second pacing acceptance statistics. No 15-minute soak was started; its entry criterion was not met.

## Independent review

Independent source, ordering, lifecycle, ownership, and GPT-6 Luna review identified unresolved blockers:

| Review area | Finding |
|---|---|
| Accepted-source ownership | **BLOCKER:** ordinary Present suppression precedes committed required-snapshot ownership. |
| Async-copy fail-open | **BLOCKER:** required snapshot failure clears the retained original and cannot replay it; the optional history-copy injection does not test this path. |
| Presentation commit | **BLOCKER:** frontier waits for positive timestamp and GPU completion; successful submit is not the commit point, and zero feedback can be retried. |
| Output ordering | Static adjacency logic looks chronological; acceptance remains unverified with zero B2 presentations. |
| Display-link lifecycle | Bypass/restart code exists; the smoke still aborts on ordinary `nextDrawable`, and no successful runtime transition was observed. |
| Resize/fullscreen/teardown | **BLOCKER / NOT TESTED:** reservation-callback race and unsafe teardown outcome remain; no lifecycle matrix evidence. |
| Metal lifetime | **BLOCKER / NOT VERIFIED:** required-snapshot failure is unrecoverable, and generator queue drain is not demonstrated. |
| DXGI-facing progression | Three sampled app Presents returned `S_OK`; the short crash run does not prove long-run unchanged progression. |
| Failure injection | **BLOCKER:** only optional history-copy completion was freshly injected; no source terminal output followed. |

The independent GPT-6 Luna review covered the patch before the final bypass-detach guard. A follow-up review checked that final delta and the retry: it confirmed the exception and two missing source terminal records remain, and found no native bypass/detach event proving the new guard executed. The review ran at **Max** effort, the highest supported effort exposed for GPT-6 Luna; an “Extra High” setting was not available. All blocking findings remain unresolved.

## Classification and next recommendation

**FAIL.** This result fails source safety and recovery. It is not a PARTIAL PASS: lifecycle correctness has not been established, and the smoke aborts before proving that the accepted source survives the optional-copy failure through actual presentation.

Keep the patch local. Before another acceptance attempt, redesign ownership so the original Presenter is suppressed only after immutable replayable GPU state is committed in known-good ordering; guarantee replay even if the required copy fails; remove the `nextDrawable` conflict from every fail-open path; and resolve stop/callback/generator-queue lifetime races. Then run the required and optional copy-failure injections first, followed by the full failure and lifecycle/teardown matrices. Only if every accepted source has a safe terminal result and those gates pass should a fresh 60-second run begin; run the 15-minute soak only after that run passes correctness.

Stop at Step 10B.2. Do not implement RIFE or prepare an upstream DXMT contribution.
