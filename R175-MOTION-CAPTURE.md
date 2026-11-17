# FG-Metal R175 — Moving-Subject Capture and Replay Safety

Date: 2026-11-17 (Asia/Baku)
Scope: audit of the preserved R157/R158 bounded capture path and an isolated, integration-ready player-ROI capture POC under `experiments/metalfx_frame_interpolator_R93/r175-evidence/capture-poc/`. No shared runtime source or Highball state was changed. The POC uses synthetic test textures only; no game was launched and no live capture was attempted.

## Decision

**R158 remains the bounded capture foundation; the isolated R175 POC is not integrated or live-approved. Do not authorize live capture on the current runtime.** R158 corrected R157's busy-pair abort and preserves the active job within the existing bounds. Its operator arm still selects only fixed bars and compass ROIs; its manifest lacks source-frame timestamps and the `deltaTime` used for generation. The new POC adds a fixed player ROI and provisional pair metadata only at a standalone helper seam. It does not establish runtime timestamp-to-texture binding, authentic MetalFX A/M/B association, or full-input replay.

The evidence packages are preserved in separate worktrees: R157 at `/Users/nima/.codex/worktrees/r157-hud-capture` and R158 at `/Users/nima/.codex/worktrees/r158-hud-capture`. R158’s independent pre-live review and test results apply to that exact HUD-only candidate. They do not approve an R175 ROI, new timestamp plumbing, full-input readback, or a different candidate.

```text
R175_EXISTING_CAPTURE_PATH = R158_BOUNDED_HUD_ROI_ONLY
R175_R157_BUSY_ADMISSION = R158_SAFE_SKIP_VALID_BUSY_CANDIDATES
R175_R158_AUTHENTIC_GAME_AMB = NONE
R175_PLAYER_ROI_SUPPORTED_IN_R158_RUNTIME = NO
R175_PLAYER_ROI_SUPPORTED_BY_ISOLATED_POC = YES
R175_SOURCE_TIMESTAMPS_IN_R158_MANIFEST = NO
R175_ACTUAL_DELTATIME_IN_R158_MANIFEST = NO
R175_PROVISIONAL_PAIR_METADATA_IN_POC_MANIFEST = YES
R175_AUTHENTIC_GAME_AMB_CAPTURE = NONE
R175_CONTROLLED_REPLAY_FROM_CURRENT_ROI_CAPTURE = NO
R175_REPLAYABLE_FULL_INPUT_CAPTURE = NOT_IMPLEMENTED_OR_VALIDATED
R175_TERRA_PRELIVE_APPROVAL = PENDING_FOR_EXACT_INTEGRATION_CANDIDATE
R175_LIVE_CAPTURE = NOT_AUTHORIZED_OR_PERFORMED
```

## What R157 and R158 establish

### R157 bounded readback

R157 creates a group only for the actual SOURCE A, MetalFX INTERNAL M, SOURCE B tuple before normal R123 resolve. It checks adjacent source ordinals, exact sequence-to-pair identity, equal runtime epoch, distinct textures on one device, matching dimensions, expected formats, and successful producer command-buffer completion. The export is raw ROI bytes plus a manifest; it does not read the title drawable or put capture work on the presentation pump.

Its live request accepts exactly four typed fields (`session`, `pid`, `roiPreset`, `maxGroups`) and pins `roiPreset` to `R149_FIXED_1470x956_V1`. The production controller calls `R157HudCaptureCreateJob`, which maps the two fixed bars and compass regions; `R157HudCaptureCreateJobWithROIs` accepts two rectangles as a helper but the operator request does not expose a player rectangle. See `R157HudCapture.m:835–855, 737–756, 1394–1435` in the [R158 worktree source](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/R157HudCapture.m).

The inherited limits are one active readback, at most three groups per fresh operator arm, a five-second arm window, a separate three-second readback completion deadline, at most 24 MiB of deduplicated retained product textures, and at most 16 MiB for staging buffers plus CPU working memory. Readback runs asynchronously on an R157-owned queue. CPU reads and atomic group export happen only after a successful Metal completion callback. A timeout or cancellation invalidates publication but does not release submitted GPU-owned textures or buffers before the callback. Capture starts disarmed, and request polling, staging, and export stay off the SOURCE/present/pump hot paths. The design and test limits are detailed in [R157-CAPTURE-SAFETY-DESIGN.md](/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/r157-evidence/R157-CAPTURE-SAFETY-DESIGN.md) and [R157-CAPTURE-VALIDATION.md](/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/r157-evidence/R157-CAPTURE-VALIDATION.md).

### R158 admission correction and validation

R157 treated a second eligible pair during a single-flight readback as fatal and invalidated the admitted job. R158 first validates the arriving candidate; a valid candidate is then counted as a bounded busy skip, with no new buffer allocation, readback submission, queue entry, or mutation of the active group. Stale, malformed, or over-budget candidates remain fail-closed. This correction was tested with an event-gated real Metal readback while two synthetic candidates arrived, then with serial captures, arm expiry, timeout, stale epoch, cancellation, and resource-retention checks.

The preserved R158 M3/arm64 test reports **200 assertions passed**. R127 and R130 focused regressions passed, and the exact R158 x86_64 candidate passed a GPT-6 Luna Max pre-live review. The test’s A/M/B textures are synthetic; the independent review marks failed controller completion, duplicate controller callback, and actual MetalFX-to-capture association as partial. Thus the readback and bounds are validated at the helper/controller seam, but exact live R133/MetalFX association was not exercised by that test. See [R158-CAPTURE-IMPLEMENTATION.md](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r158-evidence/R158-CAPTURE-IMPLEMENTATION.md), [R158-CAPTURE-VALIDATION.md](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r158-evidence/R158-CAPTURE-VALIDATION.md), and [R158-INDEPENDENT-REVIEW.md](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r158-evidence/R158-INDEPENDENT-REVIEW.md).

R158’s single supervised Highball attempt did not arm capture. The operator reported a frozen game screen and the observer logged failures in a legacy source-diagnostic preflight; the agent withheld the arm request, stopped the run, and restored the bottle. The report classifies the freeze cause as unknown. There are **zero authentic R158 A/M/B pixels** and no player or HUD visual finding. This prior attempt is not evidence that the R158 controller caused the freeze; it is also not a successful capture and does not permit skipping the new R175 review gate. See [R158-REPORT.md](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r158-evidence/R158-REPORT.md) and [R158-HUD-ANALYSIS.md](/Users/nima/.codex/worktrees/r158-hud-capture/experiments/metalfx_frame_interpolator_R93/r158-evidence/R158-HUD-ANALYSIS.md).

## R175 provenance gaps

The current R158 `R157HudCaptureGroup` stores source sequence and ordinal, per-source and internal epoch, internal pair and ID, producer command-buffer identities/status/error-nil/completion times, readback times, texture metadata, crop layout, and raw hashes. It does **not** store A/B source presentation timestamps or the `deltaTime` passed to MetalFX. Producer completion times, group-admission time, and readback times are asynchronous pipeline events; they are not substitutes for the time between source inputs. The R175 manifest must record both validated source timestamps and the actual pair interval/value given to MetalFX.

R172 has a candidate source timestamp origin: `R110_SOURCE_PRESENT_ENTRY` logs a `timestampMonotonicNs` from `r105NowNs()` against the tracked drawable sequence. However, that timestamp is not propagated with the previous/current SOURCE metadata into the R157 group. Its meaning must still be checked against the exact pair, current R123 path, and intended input-cadence semantics by Agent A/Terra. Do not use SOURCE copy-completion, estimator wake, MetalFX return, WSI dispatch, or GPU execution time as the A→B interval. Source-level propagation should attach the accepted A and B timestamps to their matching sequence and epoch before asynchronous generation; then require same clock, same epoch, adjacent identities, and `timestampB > timestampA` before deriving the interval.

The active R172 provider inputs are full-resolution color textures (`RGBA16Float`), depth (`R32Float`), and motion (`RG16Float`); the output is `RGBA16Float`. The capture group currently stores only the raw SOURCE textures and generated output crop. It does not store full color/depth/motion inputs or a reproducible provider history. R158 therefore captures what one current output region looks like, but it cannot replay MetalFX under another `deltaTime`.

Agent A's provisional metadata contract is represented in the POC but is not yet joined to runtime texture ownership. It records `seqA/B`, `ordinalA/B`, `epochA/B`, `sourcePresentEntryMonotonicNsA/B`, `CLOCK_MONOTONIC`, present method/timing arguments, whether an A/A history seed occurred and its optional seed dt, the actual transition dt, and its source (`fixed_baseline` or `pair_present_entry_delta`). It requires `seqB > seqA`, adjacent ordinals, equal nonzero epochs, increasing timestamps, supported ordinary-present semantics, and finite positive actual dt; for pair-derived dt, the encoded float must equal the timestamp interval converted to float. Sequence adjacency is intentionally not required because pump-mode history accepts adjacent present ordinals. Hook-entry timestamps describe title present-hook call arrival before R123/WSI dispatch, not render completion or `presentAtTime:`'s requested presentation time. This schema remains prototype-only until runtime records are immutably bound to matching drawable identity, sequence, and acquisition epoch and the races listed in the pre-live gate are resolved.

## Smallest defensible R175 design

### If the offline differential is already decisive

If Agent A’s controlled offline comparison establishes a reproducible visual change and a new independent review approves the capture design, the smallest live product check is **one** operator-selected, fixed player rectangle around a centered player during modest movement with a mostly steady camera. Keep one group as the initial budget, and cap the session at three only if review identifies a concrete need. Capture actual A/M/B raw pixels and preserve R158’s pair/epoch, producer-success, readback-completion, single-flight, texture-retention, staging, cancellation, and atomic-publication checks. Record the actual rectangle against measured texture geometry. Reject out-of-bounds/mismatched crops and invalid timestamps; do not detect or track the character. This establishes the baseline player-region output for visual analysis, but it does not alone attribute any change to `deltaTime`.

The arm request needs a strict, fresh, typed ROI specification checked against actual A/M/B dimensions and the same texture/device/format identity. Do not let a caller select arbitrary full-frame work under the old HUD preset. No capture may start from environment defaults or a stale arm request.

### If a direct `deltaTime` replay is needed

A player A/M/B crop cannot be replayed faithfully: MetalFX still requires the complete original input dimensions, full current and previous color, actual depth and motion textures, matching configuration, and equivalent history/warmup. The smallest replayable artifact is therefore a **finite input snapshot**, not an ROI-only crop. At the R158-observed 1472×956 geometry, the logical raw payload for one pair is about **53.7 MiB** if it includes raw SOURCE A/B (4 bytes/pixel each), provider color A/B (8 bytes/pixel each), depth (4 bytes/pixel), motion (4 bytes/pixel), and M (8 bytes/pixel). This estimate excludes texture allocation padding, aligned staging rows, manifests, and any warmup/history frames; remeasure actual `allocatedSize` and all staging/output sizes before accepting it.

Before deciding to build this larger capture, verify that two fresh interpolators can reproduce the captured baseline M from the exact input snapshot under equivalent reset/warmup state. If they cannot, the snapshot needs the specific prior input sequence required to reconstruct that state, with a finite sequence and byte ceiling. If equivalent state cannot be reproduced within a small bound, stop the replay plan and report the evidence limitation. A mismatch between captured M and a fixed-`1/60 s` replay makes the `deltaTime` comparison inconclusive.

If the test remains justified, add one bounded capture mode on the same asynchronous, default-off R158 controller: preserve raw input formats without transforms; record all provider settings, A/B identities, source timestamps, pair interval, actual `deltaTime`, history/warmup identity, and exact output M; use one in-flight group and a small finite group/output-byte budget. Compute total retained allocations and aligned staging before allocation. Keep GPU resources until the actual callback even after timeout; stream export on the processing queue after successful completion; publish the complete files and manifest atomically. This is a new safety envelope: R157’s 24 MiB texture and 16 MiB staging/CPU limits are not large enough for the full-input snapshot and must not be silently raised. Set reviewed finite caps only after measuring exact M3 allocations, output volume, and working memory.

## Isolated player-ROI POC and offline evidence

The prototype is isolated in [capture-poc](/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/r175-evidence/capture-poc/README.md). Its files are a review artifact, not shared runtime changes. The prototype fixes the ROI to `(480, 222, 512, 512)` over the nominal `1472x956` source geometry and accepts one group per arm. Its exact arm schema is the four fields `{session, pid, roiPreset, maxGroups}`; the session and PID must match, the preset is `R175_PLAYER_CENTER_512x512_V1`, and `maxGroups` must equal one. There is no arbitrary ROI or full-frame fallback.

The helper validates every A/M/B texture's actual dimensions, ROI bounds, device, type, storage, allocation, format, successful producer completion, and group-to-internal pair/epoch identity before readback. `R175PlayerCaptureCreateJobIfAdmitted` calls bounded state admission before job or staging construction. A valid arriving pair while the single flight is occupied returns `BusySkip` and allocates neither job nor staging buffers. If cancellation races after the blits and callback are installed, the command buffer is committed; the callback suppresses export and clears the POC's app-owned group and entry references. Pre-submit failure can safely release because no asynchronous ownership has been established.

M3 measured `allocatedSize` for the actual fixture textures was **5,652,480 B A + 11,304,960 B M + 5,652,480 B B = 22,609,920 B retained** (under the unchanged 24 MiB/25,165,824 B R158 cap). One group's ROI readback uses **4,194,304 B aligned staging + 4,194,304 B compact pixel rows + 65,536 B CPU reserve = 8,454,144 B** (under the unchanged 16 MiB/16,777,216 B R158 staging/CPU cap). The component crops are A 1,048,576 B, M 2,097,152 B, and B 1,048,576 B. These are measured for the tested device/format/geometry; runtime must recalculate actual Metal allocation sizes and fail closed above either original R158 cap. The cap was not increased.

The focused test was built and run from the POC directory with `./build-and-test.sh` (Xcode `xcrun clang`, Foundation and Metal). Result: exit 0, **88 assertions, 0 failures**. The self-test covers request schema/token/PID, metadata timing and seed cases, timestamp and ordinal ordering, fixed ROI/layout, arithmetic overflow and budget refusals, state/group/internal epoch identity, state cap, allocation-free busy skip, A/M/B raw export and manifest completeness, stale-epoch publication suppression, and cancellation with an event-gated late Metal callback. Output is preserved in `capture-poc/selftest-results.txt`. The run prints `CAPTURE_POC_POST_CALLBACK weakJob=1 weakA=1`: the test verifies the job's public group reference is cleared after the processing callback, but it does not claim the Metal implementation releases its own completed command-buffer/texture references at that exact moment. All fixtures are synthetic. No actual MetalFX controller association or authentic player pixels are established.

SHA-256 for the exact isolated review artifacts:

| File | SHA-256 |
| --- | --- |
| `capture-poc/R175PlayerCapture.h` | `d8aab7801f5b1f98d9ae59c33f384d37df580314ebffe8585a078f4dac5d536a` |
| `capture-poc/R175PlayerCapture.m` | `754b141ef196394da93e2313e80b873d4c6b2d82f17431daa4b3deff0eab8889` |
| `capture-poc/R175PlayerCaptureSelfTest.m` | `df3dd00178e707ad2231beccb5448955f6ad429bbde7513027a71dee21872dde` |
| `capture-poc/build-and-test.sh` | `ad942e595770aabcca73a02d611342a4bcad036509e38f044df6124b181feaa7` |
| `capture-poc/README.md` | `8b25a81993c0dd003fa73f7b3834f54927b02bfd835af8c75739b598eae43efc` |
| `capture-poc/selftest-results.txt` | `23d1dae296a0222e8881b3db2fd593375c0cf8061db457289d0f67063985d78c` |
| `capture-poc/r175_player_capture_selftest` | `84735e6e6d57389e071205ae07314fc5efcc38f54de316b6456baf402ad298b0` |

The R157/R158 sources and tests remain preserved in their separate worktrees and were not edited. The POC does not alter their admission policy or limits. This report and the isolated files still need parent inspection and a fresh Terra High pre-live review of the exact integration candidate before any runtime integration or gameplay capture.

## Pre-live gate and operator plan

No gameplay test is justified until the following steps are complete:

1. Choose ROI-only evidence or replayable inputs based on Agent A’s offline result. Validate source-time provenance and pair-derived `deltaTime` separately.
2. Review and integrate the isolated POC only after checking the exact source and the controller's timestamp/pair ownership join. Preserve R158’s busy skip, single-flight behavior, default-off arm, texture ownership, cancellation, and R123/R172 presentation behavior. Do not activate R136.
3. The isolated M3 POC test currently passes 88 assertions covering strict ROI/admission validation, source timestamp ordering and sequence/epoch binding at the helper seam, byte/allocation limits, actual asynchronous Metal readback, export completeness, stale-epoch suppression, busy-skip allocation counters, and cancellation with a delayed Metal callback. This does not test the production controller, authentic MetalFX A/M/B provenance, duplicate controller callbacks, or full-resolution inputs. Keep the synthetic-fixture label. Any integration candidate still needs focused controller/race tests, actual MetalFX association evidence, and relevant R127/R130/history/control regressions.
4. Build a separately named candidate from a frozen source snapshot; hash the candidate and all inputs. Run relevant R127/R130 and history/control regressions. Verify diagnostic-off behavior has no extra GPU work or hot-path reads/waits.
5. Obtain a fresh Terra High pre-live review of the exact source, candidate, cap accounting, cancellation, texture lifetime, queue behavior, and staging/restoration plan. Historical R158 Luna Max approval is insufficient for this R175 design.
6. Only after that approval, run a fresh read-only Highball/Wine/bottle/process/engine/provenance and disk-capacity check. Stage only with the validated journal helper and preserved current engine metadata. The user is available and has offered a supervised live test, but that invitation does not replace the required exact-candidate Terra approval. Once approved, one supervised offline session may use a centered player, modest movement, and at most one capture group initially. Stop on any invalid admission, mismatch, readback failure, or renewed freeze; do not retry or fall back to R136. Restore only journaled state after processes stop and save receipts.

The R158 restore receipt is historical and proves restoration of the bottle used in that 2026-11-13 attempt; it does not establish the current Highball engine or bottle state for R175. Recheck it before any future staging. No Highball status was inspected and no live capture was performed.

## Recommendation

Proceed with the offline `deltaTime` differential first. If it has a clear effect, the isolated POC is the smallest proposed live check: one authenticated, fixed player-region A/M/B group under the existing R158 memory ceilings. It remains a candidate pending code review, runtime integration, and fresh Terra High pre-live approval. Treat any resulting group as quality inspection only unless a full-input, history-equivalent replay is proven. Expand to one bounded full-input snapshot only when the offline result cannot answer whether the correction matters to actual gameplay and replayability can be established without unbounded history capture.
