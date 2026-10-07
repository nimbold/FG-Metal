# Step 10B.6.1 — matched fullscreen F gate and windowed-path isolation

Captured 2026-10-04 on the same Apple M3 / built-in 60 Hz display as Step 10B.6. Local downstream experiment only.

## Final result — CASE B; architecture STOP

**WINDOWED DXMT: FAIL / NOT CLEARED. FULLSCREEN DXMT: FAIL (presentation health; source safety passed). RIFE: NOT CLEARED. DXMT architecture: STOP.**

**Primary result: CASE B.** The fullscreen eligibility override ran safely, but the unmodified-property fullscreen F path remained materially abnormal. The scale-only probe produced a strong qualified short improvement, then failed the fresh 60-second validation. No RIFE, Highball, scheduler, generator, SourceEscrow design, or production capability change was made.

## Candidate and default gate

- DXMT base commit: `fb4515681daefb789a4d0f403c4bdbca88f3b3de`.
- Retained Step 10B.6 source-tree identity: `664aaad0d4611f8272d9d2857e236cce2101dd012fb2daea08741765e6608551`; original source copy remains untouched. `frozen-source-files.json` records all 478 files.
- Base local delta SHA-256: `6a69545b928607cd6cf777ce230b58c77d4dde18bcb9f97a4f144ef53def3b17`; see [local delta](local-experiment-delta.diff). Exactly three downstream files changed.
- Base primary D3D11 DLL: `5820cce603eee6a7cd04e8f8e51b69de8fc21d605a5a17ab4890cbc5d7f76f02`; WineMetal SO: `38dcbef55964fd100f0eb0a0497fca39472f80badd19a9031149c5e90a9426a0`. These are retained as `base-d3d11.dll` / `base-winemetal.so`.
- Shared matched harness: `b682fd40608816be004f9ea2476d2147a8e1e244a392dc709966e1e200450d74`.
- Wine and wineserver remain the frozen Step 10B.6 binaries. Every run records complete runtime hashes in `run-identity.json`; primary A/B identities match exactly.
- The original static archive was already absent. Rebuilding unchanged core source produced `43fd2e89199e9b22e758c2cc5d0e84fb8062c5a6060575bdc7809d608c8f91cf`, not the old `dbed8452d8458641aee9ca6b58188ba5a3b4873ff2e0dfd357143fafade19d54`. This is a new matched candidate, not a bit-identical Step 10B.5 rebuild. All frozen core/synthetic source hashes match.

The original `!fullscreen_desc_.Windowed` restriction has no documented demonstrated fullscreen texture/layer incompatibility in the frozen source or initial downstream patch. Review found it to be a narrow experimental capability gate. Earlier lifecycle tests exercised fullscreen entry/exit while a Framegen state existed but automatically handed off to ordinary presentation in fullscreen; they did not test active fullscreen Framegen.

The local eligibility change retains all existing conditions: D3D11 without MetalFX, SDR RGB G22 P709, one sample, RGBA/BGRA8, `SyncInterval=1`, zero flags and no Present parameters. Fullscreen additionally requires both `DXMT_FRAMEGEN_EXPERIMENT_ALLOW_FULLSCREEN=1` and `DXMT_FRAMEGEN_EXPERIMENT_CONTROLLED_HARNESS=step10b6_1`. Absent the opt-in, fullscreen still rejects Framegen. The default-gate negative control entered fullscreen with the opt-in absent, logged `source_supported=0`, and made no Framegen session/native.csv. Its runner exit 1 is the expected missing-active-session failure; the D3D app exited 0. It is not a qualified F run.

## Two fresh sources and transition scope

The frozen path previously claimed the layer after one priming source. The experimental admission guard now requires two adjacent current-epoch records that are committed, non-cancelled, both snapshot/history GPU-complete and nonfailed, with identical device, dimensions, pixel format, and one sample, before `resumeDelivery`. It is applied identically in both A/B modes and does not alter the F timing/selection algorithm. It deliberately does not require generation_allowed so source-only mode remains admissible.

Controlled sequence: create an ordinary window/swapchain → enter fullscreen before the first Present/session → pump 3 seconds → `ResizeBuffers(640,360)` in both modes → pump 1 second → create fresh Framegen epoch lazily on the first supported Present → accept/complete two ordinary priming copies → log proof → claim layer → attach display link → activate F. Primary fullscreen native rows show epoch 2, source 1 and 2 escrow completions, pair 1:2 proof, claim, display-link attach and ACTIVE in that order. No windowed source or generated history exists before this cold entry.

SetFullscreenState/ResizeTarget change display mode and Win32 style/geometry on the same Presenter/WineMetal layer; ResizeBuffers changes backbuffers/layer props. This is not AppKit toggleFullScreen. Active-owner transitions quiesce callbacks, worker drawables, accepted sources and GPU work, reset epoch, and release owner. **Not established:** non-owner priming-history mode transitions or concurrent Present/mode transition safety. The reviewers identified early-return history retention and a gap between quiesce completion and mode mutation; the single-thread cold-entry harness avoids them. No general mode-reentry support claim follows.

## Methodology and qualification

Same deterministic four-color per-Present workload, harness binary, 30 Hz source pacing, F policy, source/drawable 640×360, DLL/Wine hashes, fixed 60 Hz display, latency 1, signposts and Metal System Trace + Display + Points of Interest profile. Each run used a new disposable engine and Wine prefix. Native controls were retained, not rerun.

The supervisor waits for the frame-90 geometry/visible/foreground marker and for completed two-source proof < claim < ACTIVE in one epoch before recording. Post-trace qualification requires generation present, circuit 0, one epoch, no claim/release/drain/reset in the aligned interval, complete feedback, clean source/GPU ledger, and app foreground/visible throughout that interval plus one-second margins. App QPC elapsed time is correlated to Mach using the first 20 reservation/app pairs before fallback can change source-ID correspondence. Complete-run visibility is reported separately.

An initial capture supervisor could not identify Wine after its argv changed; the aborted `matched-windowed` run is unqualified. PID selection now uses the launch runner’s direct-child lineage. `matched-windowed-v2` qualified, then the methodology review requested an explicit post-resize settle; the final A/B pair uses the revised shared binary. Earlier results are retained as controls/superseded evidence, not pooled with the primary pair.

## Live public layer/window getters

Values below are actual 6/15/25-second runtime samples, except the native column retained from Step 10B.6. Diagnostics run on the main queue and only read public getters. The first fullscreen setter briefly logged a 1470×956 drawable before the explicit resize; all settled/measurement getters are 640×360.

| Getter | Native control | Matched windowed | Matched fullscreen |
|---|---|---|---|
| Device | Apple M3; registry 4294968387 | Apple M3; 4294968387 | Apple M3; 4294968387 |
| pixelFormat | 80 / BGRA8Unorm | 80 | 80 |
| drawableSize | 640×360 | 640×360 | 640×360 |
| contentsScale | 2 | 1 | 1 |
| Layer opaque / framebufferOnly / displaySync | true / false / false | true / false / false | true / false / false |
| maximumDrawableCount / timeout / transaction | 3 / true / false | 3 / true / false | 3 / true / false |
| colorspace | kCGColorSpaceSRGB | kCGColorSpaceSRGB | kCGColorSpaceSRGB |
| EDR wants content / current screen max | false / SDR | false / 1 | false / 1 |
| Screen potential EDR | not recorded | 2 | 2 |
| NSWindow styleMask | 15 | 15 | 0 |
| NSWindow frame | 640×360 content | {{4, 531}, {640, 392}} | {{0, 0}, {1470, 956}} |
| Native content size / layer bounds | 640×360 points | {{4, 531}, {640, 360}} | {{0, 0}, {1470, 956}} |
| Window backing scale / backing type | 2 / raw 3 | 2 / raw 3 | 2 / raw 3 |
| Window opaque / visible / key | true / visible / active | true / true / true | true / true / true |
| Occlusion state | not recorded | 8194 | 8194 |
| Parent layer opaque | true (native source explicitly sets it) | False | False |
| View / parent / window class | NativeMetalChildView / NSView / NSWindow | WineMetalView / WineContentView / WineWindow | WineMetalView / WineContentView / WineWindow |
| Display ID / screen | 1 / built-in | 1 / Built-in Retina Display | 1 / Built-in Retina Display |

The JSONL also records actual object pointer identity, device registry ID, layer/view bounds/frame, colorspace description, window background, miniaturization, visibility and occlusion. Window opacity is actually true in both modes; the pinned Wine initialization value opaque=false is not the live cause. Fullscreen layer/view/window geometry is 1470×956 points while its drawable/source remains 640×360. This is a matched source geometry across DXMT modes, not a geometric match to the native 640×360-point window.

## Native/windowed/fullscreen comparison

DXMT numbers below describe aligned CA request intervals. Native rows are retained calibrated controls. Positive feedback is public framework feedback, not physical panel sensing. Surface rows are **not FPS**.

| Run / interval | App Presents/s | Callbacks/s | Submissions/s | GPU completions/s | Positive/s; count / zero | Surface rows; rows/s | Direct to Display |
|---|---:|---:|---:|---:|---|---|---|
| Native 60-changing / 20.666 s | n/a | ~60 | ~60 | ~60 | ~60; 1241 / 0 | 773; 37.525 | Yes 772 / No 1 |
| Native 30→60 / 20.583 s | 30-source synthetic | ~60 | ~60 | ~60 | ~60; 1236 / 0 | 683; 33.317 | Yes 682 / No 1 |
| matched-windowed-final / 20.616 s | 30.025 | 59.952 | 59.904 | 59.904 | 57.139; 1178 / 57 | 173; 8.467 | No 173 |
| matched-fullscreen-final / 20.650 s | 29.975 | 59.661 | 59.660 | 59.660 | 57.917; 1196 / 36 | 240; 11.755 | No 240 |
| property-control / 20.667 s | 30.000 | 59.807 | 59.855 | 59.855 | 59.709; 1234 / 3 | 222; 10.945 | No 222 |
| property-scale-2 / 20.650 s | 30.024 | 59.952 | 59.903 | 59.903 | 59.903; 1237 / 0 | 1231; 59.806 | No 1231 |
| property-parent-opaque / 20.650 s | 29.975 | 59.952 | 59.902 | 59.902 | 59.805; 1235 / 2 | 241; 11.718 | No 241 |
| scale2-validation-60s **UNQUALIFIED** / 60.733 s | 30.033 | 59.605 | 59.572 | 59.572 | 58.634; 3561 / 57 | 713; 11.772 | No 713 |

The primary windowed/native feedback alignment residual p95/max is 0.456/3.924 ms; fullscreen is 0.426/1.996 ms. The long validation has a 200.108 ms maximum residual despite a 0.432 ms p95. Its callback/submission/positive-zero join is diagnostic, not a qualified matched ledger. Treat its failure-to-qualify and native full-run ledger as stronger evidence than its per-request aligned counts. App rates use the stated QPC/Mach correlation.

| Run | Surface Duration p50 / p95 / p99 / max, ms | CPU-to-display latency p50 / p95 / p99 / max, ms |
|---|---|---|
| native-motion60 | 33.333 / 33.333 / 33.333 / 33.333 | 48.460 / 49.128 / 49.282 / 49.703 |
| native-source30-to60 | 33.333 / 33.333 / 33.333 / 83.332 | 48.383 / 48.866 / 49.077 / 49.895 |
| matched-windowed-final | 116.665 / 316.663 / 463.995 / 516.661 | 49.874 / 66.486 / 66.582 / 82.963 |
| matched-fullscreen-final | 91.666 / 183.331 / 264.664 / 333.330 | 65.810 / 82.459 / 82.789 / 83.141 |
| property-control | 116.665 / 183.331 / 333.329 / 333.330 | 65.418 / 66.525 / 79.488 / 83.228 |
| property-scale-2 | 16.666 / 16.667 / 16.667 / 49.999 | 49.006 / 49.827 / 49.893 / 49.950 |
| property-parent-opaque | 16.667 / 183.331 / 333.330 / 333.330 | 65.912 / 66.580 / 66.616 / 66.632 |
| scale2-validation-60s (unqualified) | 49.999 / 183.331 / 299.997 / 349.996 | 65.771 / 81.764 / 82.772 / 99.300 |

Direct-to-display failure reasons: primary windowed display rejected=166, empty=7; primary fullscreen rejected=196, empty=44; control rejected=182, empty=40 (see exact exported distribution for each run); scale-only rejected on every 1231 row; parent-only rejected=184, empty=57. The text belongs to the Display direct-path failing-reason field, not the Metal present() result. Full distributions and frame-label-step histograms are in [comparison.json](comparison.json). Native median frame-label step is 2; primary windowed median 6 and fullscreen median 4. Raw pooled window labels include a -451/+463 discontinuity; these are not application drawable counters or counts of drops.

| Run | Built-in VSync/s | Frame-label step p50 / p95 / p99 / max |
|---|---:|---|
| matched-windowed-final | 59.903 | 6.000 / 19.450 / 30.000 / 463.000 |
| matched-fullscreen-final | 60.001 | 4.000 / 12.000 / 16.340 / 21.000 |
| property-control | 59.951 | 6.000 / 12.000 / 21.000 / 21.000 |
| property-scale-2 | 60.001 | 1.000 / 1.000 / 1.000 / 3.000 |
| property-parent-opaque | 60.001 | 1.000 / 12.000 / 21.000 / 21.000 |
| scale2-validation-60s | 59.968 | 4.000 / 12.000 / 17.890 / 22.000 |

Complete discrete label and failing-reason distributions are retained in comparison.json. The long-run row is diagnostic only because qualification failed.

## Source safety and A/G/B

| Run | Ownership-accepted IDs | Unique safe terminals | Lost / unsafe / regression / malformed G / GPU failed | Full-run owner claims |
|---|---:|---:|---|---:|
| matched-windowed-final | 1496 | 1496 | 0 / 0 / 0 / 0 / 0 | 2 |
| matched-fullscreen-final | 1499 | 1499 | 0 / 0 / 0 / 0 / 0 | 1 |
| property-control | 1199 | 1199 | 0 / 0 / 0 / 0 / 0 | 1 |
| property-scale-2 | 1182 | 1182 | 0 / 0 / 0 / 0 / 0 | 6 |
| property-parent-opaque | 1199 | 1199 | 0 / 0 / 0 / 0 / 0 | 1 |
| scale2-validation-60s | 2238 | 2238 | 0 / 0 / 0 / 0 / 0 | 4 |

All accepted sources have exactly one safe terminal; no competing ordinary nextDrawable ownership assertion occurred. Initial ordinary-fallback priming terminals are not accepted Framegen ownership IDs. The primary windowed app has one reserve/setup failure and an earlier owner handoff outside its stable epoch-3 measurement; primary fullscreen is one stable epoch-2 run and cleanly drains on exit. The short scale run has seven later history-exhaustion/source-only events, six owner claims, and a foreground lapse beginning at app sequence 1076 / 36.28 seconds, outside the 4.96–25.61 second aligned window. Its measured interval plus one-second visibility margins qualifies; its whole 40-second app run does not establish steady operation.

| Qualified interval | G submitted | Positive / zero G | Exact adjacent pair malformed | Both endpoints positive / A<G<B | Missing positive A / B | Positive G after recorded B target |
|---|---:|---|---:|---|---|---:|
| matched-windowed-final | 601 | 591 / 10 | 0 | 503 / 502 | 45 / 44 | 114 |
| matched-fullscreen-final | 608 | 590 / 18 | 0 | 557 / 557 | 17 / 16 | 146 |
| property-control | 616 | 615 / 1 | 0 | 611 / 611 | 2 / 2 | 128 |
| property-scale-2 | 616 | 616 / 0 | 0 | 616 / 616 | 0 / 0 | 0 |
| property-parent-opaque | 617 | 615 / 2 | 0 | 615 / 615 | 0 / 0 | 143 |

| Full app run | G requested / ready / selected / submitted | Positive / zero G feedback | Strict nearest-source brackets / immediate A/G/B triples |
|---|---|---|---|
| matched-windowed-final | 1496 / 1496 / 1458 / 1458 | 1436 / 22 | 1230 / 1230 |
| matched-fullscreen-final | 1499 / 1499 / 1480 / 1480 | 1461 / 19 | 1417 / 1417 |
| property-control | 1199 / 1199 / 1192 / 1192 | 1189 / 3 | 1184 / 1184 |
| property-scale-2 | 1174 / 1163 / 1143 / 1143 | 1142 / 1 | 1135 / 1135 |
| property-parent-opaque | 1199 / 1199 / 1187 / 1187 | 1183 / 4 | 1170 / 1170 |
| scale2-validation-60s | 2229 / 2223 / 2192 / 2192 | 2167 / 25 | 2093 / 2093 |

Primary fullscreen full-run endpoint chronology: 1422 positive G records had both source endpoints, and all 1422 were A<G<B; missing A=20 / missing B=19. There were 408 positive G feedback records after B’s recorded display target, even though none were after B’s positive feedback. The qualified interval likewise has 146 after-target G results. This is a pacing limitation, not malformed pair IDs. Primary windowed has one positive G before its A endpoint in the full run (1255/1256 ordered cases); it is retained as an IMPORTANT chronology anomaly. Short scale-only has all 616 aligned G feedback positive, exact pairs, A<G<B, no missing endpoints, and zero after-B-target G. The A<G<B table uses existential ID matching across all positive feedback for each endpoint: it does not require the immediately neighboring displayed sources. The separate strict nearest-source/immediate-triple counts above expose that distinction. Repeated endpoints and missing feedback remain explicit; none of these framework timestamps proves physical scanout.

## Two public-property experiments and longer validation

Both hypotheses were specified before running; see [hypotheses](phase13-hypotheses.json) and the [additional local delta](phase13-local-delta.diff). The same amended dispatcher binary was used for selector-absent control and both treatments; complete identities are retained.

1. `contentsScale=2` only: runtime getter equals native/backing scale, parent opacity stays false, all geometry/other getters match control. Qualified ~20.65 s: 1237 positive feedback / zero zeros, surface Duration p50/p95 ~16.666 ms, CPU latency p50 ~49 ms. Direct to Display stays No. This is a genuine short statistical improvement, not proof of physical scanout or a durable supported lane.
2. Parent backing-layer opaque=YES only: contentsScale stays 1, geometry/other getters match control. Median Duration shortens to ~16.666 ms but p95 remains 183.331 ms / p99 333.330 ms; only 241 rows versus control 222, all direct-path No. Mixed signal; it does not repair the long-tail/direct-path concern. The properties were never combined.

The scale-only candidate then ran a fresh 75-second app with a requested 60-second trace (60.733-second aligned request span). Every app Present stayed visible/foreground; source contract/GPU accounting stayed clean. **Validation FAIL:** multiple measured epochs (4 and 5), circuit states 0 and 1, owner drain/release/reclaim inside the measurement, and five history-exhaustion setup/fallback events over the full app run. Surface Duration returned to p50 ~50 / p95 ~183 / p99 ~300 / max ~350 ms, all 713 rows direct-path No. Full native run: ~30.16 source Presents/s, ~59.65 callbacks/s, 4432 GPU-completed submissions, 4366 positive / 66 zero feedback. This cannot be promoted to a stable 60-second F lane.

The conditional fullscreen disable/re-enable, source-only, generator-submission-failure, deadline-miss and additional active-exit regression matrix was **NOT RUN** because the required longer fullscreen validation did not pass. Normal active exit/quiescent teardown is present in the completed primary/property logs; that does not substitute for the requested failure matrix. No RIFE implementation was started.

## Review reconciliation

Preimplementation independent reviews covered fullscreen/DXGI semantics, SourceEscrow, Wine/native hierarchy, calibrated Display interpretation, and methodology. Blocking one-source admission was resolved by the two-fresh-source experimental guard. Post-resize settling, pretrace event ordering, circuit/generation qualification, and measured-window visibility checks were strengthened following review. Remaining non-owner/concurrent-transition risks are not cleared by the cold-entry run.

Six separate final read-only GPT-6 Luna Extra High reviews were completed:

| Review | Classification and reconciliation |
|---|---|
| Fullscreen source safety | No source-safety BLOCKER for the qualified cold-entry run: accepted IDs all have one safe terminal, exclusive-owner assertions do not fire, GPU/pair/regression ledgers are clean. FOLLOW-UP: general non-owner and concurrent mode transitions remain unverified. |
| Matched methodology | Primary A/B harness/runtime hashes, two-source proof ordering, and one-epoch qualification verified. Same-binary property control/treatments verified as a separate binary family. BLOCKER for sustained PASS: long validation contains owner/circuit/epoch transitions. IMPORTANT: fixed-order single trials cannot establish replicated causal or physical-scanout proof. |
| Native/windowed/fullscreen trace comparison | BLOCKER: both qualified primary paths remain abnormal versus native; neither direct-path status nor Duration closes the gap. Long scale validation fails qualification and has a 200.108 ms maximum alignment residual; its aligned counts remain diagnostic. NOT ACTIONABLE: surface-row rate is not FPS and zero feedback does not identify individual physical drops. |
| Live layer properties | Actual Wine window opacity is true; source initialization alone would have misidentified it. Both probes change exactly one measured public property. IMPORTANT: contentsScale=2 has a strong short effect; parent opacity has mixed median/tail evidence. All observed DXMT Direct-to-Display rows remain No. |
| A/G/B chronology | No malformed adjacent IDs. IMPORTANT: existential endpoint-ID brackets differ from strict nearest-source/immediate triples; both are now reported. One windowed G precedes its A endpoint. BLOCKER for a presentation/RIFE PASS: timing/direct-path evidence remains insufficient and long validation is unqualified. |
| Adversarial STOP/CONTINUE | STOP. Native remains healthy, both matched modes are abnormal, and the two permitted property probes do not establish a durable lane. Preserve the short scale effect without declaring it a supported fullscreen path. |

The initial one-source admission BLOCKER was resolved before runtime testing. The final presentation-health and sustained-validation BLOCKERs are **not resolved**; the outcome is FAIL/STOP, not fullscreen PASS. No third property experiment, further incremental Step 10B.*, RIFE work, or broad renderer implementation follows this step. The next decision is renderer-strategy selection.

The stop is an engineering decision under Phase 14, not a proof that every possible DXMT integration is intrinsically impossible. The exact property limit was respected; short scale-only success was tested longer and did not hold. Windowed remains uncleared, and the conditions for a fullscreen-only product lane were not met.

## Retained evidence and validation

Each run retains native.csv, native-timing.csv, app/fullscreen logs, runtime getter JSONL, run identities/manifests, safety/chronology summary, trace TOC, calibrated Display/CA XML, Metal submission/completion/error XML and signpost XML. Raw traces are retained only for the primary pair, same-binary control, both property treatments and longer validation; their paths/hashes are in raw-trace manifests. Disposable engine/prefix copies created in this step are removed after identities and exports are retained; the frozen source/runtime seed and all pre-existing user files are preserved.

Build verification: native x86_64 WineMetal + PE DXMT Meson/Ninja build passed; controlled harness compiled cleanly; all 15 analyzer regression tests passed. Frozen core/synthetic source hashes still match. This is local renderer/runtime evidence; it is not packaged game/Highball integration or physical panel sensing.
