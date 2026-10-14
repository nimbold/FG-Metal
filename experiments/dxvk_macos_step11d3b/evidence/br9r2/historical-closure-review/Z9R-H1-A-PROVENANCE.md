# Subagent A — Historical Evidence / Provenance

Review date: 2026-10-14. This review inspected existing records read-only. The only file created for this workstream is this report. No original run artifact was edited, moved, or deleted.

## Evidence identity

Repository root: `/Users/nima/Documents/Code/FG-Metal`.

Historical run ID: `step11d3b-baseline-01-20261014-130823-44892b9b`.

- Signed run-output directory (`RUN_DIR`): `/Users/nima/Documents/Code/FG-Metal/experiments/dxvk_macos_step11d3b/runs/step11d3b-baseline-01-20261014-130823-44892b9b/`
- Original run manifest: `RUN_DIR/run-manifest.json`, SHA-256 `e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365`.
- Storage reservation manifest: `/Users/nima/Documents/Code/FG-Metal/experiments/storage/manifests/step11d3b-baseline-01-20261014-130823-44892b9b.json`, SHA-256 `11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59`, HMAC field `manifest_hmac_sha256=14fb362b4c851228cef01eff698a89248b6a2358d4bfca97ebbfd7dd872d84fb`.
- Final run result, which contains the saved PRE and POST objects: `RUN_DIR/run-result.json`, SHA-256 `a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589`.
- Standalone PrefixLease receipt: `RUN_DIR/prefix-cleanup.json`, SHA-256 `0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308`; receipt HMAC field `6986a8ab87e89f5f7cd76908f4f485283be1158febce75e71d40df65979d0aef`.
- Original disposition text: `experiments/dxvk_macos_step11d3b/STEP-11D3-BR9R2-REPORT.md`, lines 841–896 and storage reconciliation lines 898–953.

The prefix cleanup receipt's `wineprefix_identity.manifest_sha256` binds the **storage reservation manifest** SHA above, not the separate `run-manifest.json` SHA. The manifest and both meanings must remain distinct.

## 1. What did the original manifest authorize?

The storage manifest's JSON fields are authoritative for the persistent-output reservation: `decision=ALLOW`, `experiment_id=step11d3b-baseline-01-20261014-130823-44892b9b`, `experiment_step=11D.3-B`, `estimated_persistent_bytes=314572800`, `expected_duration_seconds=300`, `expected_prefix_bytes=3221225472`, and `expected_build_bytes=0` with `expected_build_requirement=false`. It binds `output_dir` to the signed run directory above. See the manifest's lines 44–67 (especially 46, 50, 55–59, and 67). Its `purpose` is a STEP 11D.3-B bare Wine Vulkan baseline using pinned Wine 11.17, `winevulkan`, MoltenVK 1.4.2, and `WineMetalLayer`, with no DXVK/D3D/interop/scheduler. The 300-second experiment did not authorize enlarging its 314,572,800-byte persistent-output reservation after execution.

`run-manifest.json` records `phase=baseline-01`, `duration_seconds=300`, the Wine launcher and bare Vulkan app command, requested x86_64-under-Rosetta BGRA8/FIFO/three-image 640×360 window, and `storage_manifest_path` back to the signed storage manifest. It records excluded components at lines 247–257 and the original PRE at `gate0_pre` lines 260–282. Its SHA-256 is the value above.

The later derived future-run reservation of 4,974,444,544 bytes is not part of either historical manifest and cannot describe this run retroactively; the later report gives it as a future-run model at lines 939–940.

## 2. What was actually retained at Gate POST?

The saved POST object is `run-result.json:gate0_post` (lines 65–92): it records `stage=POST`, `gate0_status=BLOCKED`, `captured_at_utc=2026-10-14T14:14:51.607406+00:00`, no active builds or PrefixLeases, 81,306,374,144 free bytes, and 49,825,538,048 scoped bytes. Its exact blocker array is:

1. `signed experiment output exceeds its persistent reservation: step11d3b-baseline-01-20261014-130823-44892b9b`
2. `unrelated non-lease storage changed since Gate 0 certification`
3. The same signed-output-over-reservation blocker again.

The Gate POST object itself does not store a numeric `signed_tree_allocated_bytes` field. The saved report reconstructs the signed tree at POST as 593,119,003 logical / 598,573,056 allocated bytes, 284,000,256 bytes over the original reservation. Its category breakdown is in `STEP-11D3-BR9R2-REPORT.md:919–931`:

| Output retained in the signed run directory at POST | Logical bytes | Allocated bytes |
|---|---:|---:|
| Target host trace | 364,651,052 | 369,160,192 |
| App trace | 197,310,536 | 197,312,512 |
| Helper trace | 1,823 | 4,096 |
| Runtime/process logs | 1,179,674 | 2,105,344 |
| Copied runtime libraries | 29,865,008 | 29,872,128 |
| Trace index, metadata, and cleanup artifacts | 110,910 | 118,784 |
| **POST tree** | **593,119,003** | **598,573,056** |

The numeric POST tree amount is a report reconstruction, corroborated by the current measured tree after subtracting the later `run-result.json` allocation. It is not a numeric field inside `gate0_post`.

The standalone PrefixLease receipt is a different measurement: `status=PASS`, `prefix_deleted=true`, and active marker removal followed a durable receipt (`prefix-cleanup.json:2–4, 17–31, 49–55`). It records `expected_persistent_bytes=314572800`, `persistent_retained_allocated_estimate_bytes=575672320`, and `unexpected_persistent_bytes=261099520` (`:17–18, 26, 75`). That 575,672,320-byte figure is a whole-project retained-allocation estimate, not the signed-tree POST measurement. The two overrun differences are 22,900,736 bytes apart; the saved records do not identify that difference by path/inode. The report explicitly preserves this uncertainty at lines 906–907.

## 3. What was written afterward?

The signed run tree's `run-result.json` is timestamped `2026-10-14T14:14:51.612280+00:00` (`run-result.json:60`), 4.874 ms after the embedded Gate POST capture. It is 127,112,105 logical bytes and 127,115,264 allocated bytes. It contains final validation and the immutable copy of both gate records. The report says it was written after Gate POST at `STEP-11D3-BR9R2-REPORT.md:930`.

The resulting current tree is 720,231,108 logical bytes / 725,688,320 allocated bytes; the later `run-result.json` alone accounts for the 127,115,264-byte allocated difference from the reconstructed POST tree. The report and six workstream deliverables were also written after the original POST, but outside the signed run directory (`...REPORT.md:933`). They are project writes; they do not change the old signed reservation or explain the already-recorded POST blocker.

The controller, timeline, public WindowServer JSONL, and observer records are in the separate owned transient evidence root under `/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/`, outside the signed run tree. They currently exist and are hashed below, but their `/private/tmp` location is not the same durability guarantee as the repository run tree.

## 4. Which hashes/records are immutable historical evidence?

The following records bind the original authorization, result, cleanup, or primary traces. Preserve their bytes and field values as recorded:

| Record | SHA-256 / binding |
|---|---|
| `run-manifest.json` | `e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365` |
| Storage manifest JSON | `11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59`; HMAC `14fb362b4c851228cef01eff698a89248b6a2358d4bfca97ebbfd7dd872d84fb` |
| `prefix-cleanup.json` | `0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308`; receipt HMAC `6986a8ab87e89f5f7cd76908f4f485283be1158febce75e71d40df65979d0aef` |
| `run-result.json` (PRE, POST, final status, validation, nested cleanup receipt and trace refs) | `a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589` |
| `execution-result.json` | `dca9ab61f70d36a2790243bd2a7b058c7dbda7e09f365f1f89d1f208aab3b4f6` |
| `trace-index.json` | `1e03ffab47bf76a1d3a440b639b232cf15a9238955e4c64cfd23de2ba3694582` |
| `app-trace.jsonl` | `d57f5ec94bb2bdab51bebd87c97e3a7bc0650539dd5756897ae39a5aec87774c` |
| `host-trace.58921.jsonl` (target) | `f839fab2af872f597747a3d1d088e785b3ea4bf8237961e5377c6c49276866d2` |
| `host-trace.58942.jsonl` (helper-only) | `03c760a06e261364788571da15264887e5476188829d601bb06b98cd15e7ce5f` |
| transient `controller.json` | `1a7590578476931ceb9e816f7a1a15c507d63d6ec9abd36bcdedfec3771e638b` |
| transient `timeline.jsonl` | `680fcb513c01e2e56e442c100b33ed0df16a386c68e2f5f16669bcbe56eb63f8` |
| transient `public-cgwindow-inventory.jsonl` | `46767328e8edca805cf80de15e443785b5b3cfe167ec0b6018bf060a10a94bab` |
| transient observer `observer-result.json` | `17fb45ab4077ad13c642d0a8cb0032441f4a4797898e7245f962ae5f9f00b0aa` |
| transient observer `observer-controller.json` | `f82e9e985108281595f56ec81f833321611ae9dedcdaa1278026bd0727d97da2` |
| transient observer `observer-samples.jsonl` | `d4787506c269a74ee0d7f1099ab4234e83737df63feb53bef407121c24930603` |

The trace index records `status=PASS`, no index errors, token `11732b65ef09689e0cac94f3e0579e9c`, and target PID 58921. Its process entries bind target PID 58921 to 620,529 target records and helper PID 58942 to two helper-only records. The app/host trace hashes above also appear in `run-result.json:trace_artifacts` and `trace-index.json:processes`.

The replay classification text itself is in `STEP-11D3-BR9R2-REPORT.md:845–847, 888, 953`. It has no independent SHA field in the run-result schema; preserving the report text is necessary alongside the hashed machine records.

## 5. Which facts must never be rewritten?

Keep these original facts byte-for-byte or field-for-field, including their failure meanings:

- The storage manifest allowed 314,572,800 persistent-output bytes, and the run manifest references that original storage manifest.
- Gate PRE was PASS with its captured time, values, and empty blockers (`run-manifest.json:260–282`; duplicated in `run-result.json:93–116`).
- Gate POST was BLOCKED with its exact ordered blocker array, including the repeated signed-overrun entry (`run-result.json:65–92`).
- The original signed-tree POST reconstruction is 598,573,056 allocated bytes; the receipt's separate whole-project estimate is 575,672,320 bytes. Do not substitute one for the other.
- The original cleanup receipt is PASS, the temporary prefix was deleted, and the marker was removed only after receipt durability. Its reported unexpected persistent amount remains 261,099,520 bytes.
- `run-result.json` remains `status=FAIL`, `is_valid=false`, `prefix_cleanup_pass=true`; a successful cleanup does not convert Gate POST to PASS.
- The current surviving signed-tree allocation is 725,688,320 bytes, including the later result file. Do not hide, delete, compress, or move those historical bytes as part of a logical closure.
- The report classification remains R5 — INVALID / INCOMPLETE and T8 — INSUFFICIENT TIMING EVIDENCE. Finder treatment was not executed; replay 2 was not run.
- The later 4,974,444,544-byte reservation belongs to future runs only; it must not be inserted into this old run's manifest.

## 6. Is the run already terminal by any existing schema?

The execution is terminal in the ordinary result sense: `execution-result.json` records a finish time, target PID, return code 0, and `timed_out=false`; `run-result.json` records `status=FAIL`, `is_valid=false`, process and runner return codes 0, `timed_out=false`, and `prefix_cleanup_pass=true`. At the captured POST the active build and PrefixLease arrays were empty, and the cleanup receipt records a durable receipt before marker removal.

There is no dedicated historical terminal/closed/retired state in these original schemas. The storage manifest still says `decision=ALLOW`; the run result has no `terminal`, `closed`, `retired`, or archive certificate field; the controller separately says `INVALID_OR_INCOMPLETE`. Thus the process/result/lease lifecycle is finished, but the old reservation is not marked terminal for Gate accounting. This conclusion is limited to the inspected run, storage-manifest, result, and controller records; this provenance review did not audit every repository lifecycle schema.

## 7. What exact evidence proves R5/T8 and Gate POST failure?

**R5 / T8.** The original controller JSON at `/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/z9-replay-1-20261014-130814-7dafc87e/controller.json` records `status=INVALID_OR_INCOMPLETE`, `technical_valid=false`, `finder_actions=[]`, `finder_window_created_by_this_run=false`, `measured_phases={}`, `measured_sequence_completed=false`, `runner_returncode=1`, and a `controller_error` saying `canonical uncovered PRE precondition not proven`. At that failure it recorded `canonical_streak=null`, `stable_public_cg_identity=false`, zero public identity samples in the preceding two seconds, `UNIQUE_ONSCREEN_JOIN_FAILED`, `no_finder_full_cover=true`, and `renderer_health.healthy=false`. The full timeline at the sibling `timeline.jsonl` contains only `controller_error` and `controller_finished` events; it has no measured phase/Finder action event. This directly supports invalid/incomplete R5 and the absence of any treatment interval. The frozen analysis maps incomplete/invalid runs to R5/T8 (`z9_event_aligned_analysis.py:813–833`); the reviewed protocol defines T8 for insufficient timing or identity evidence (`Z9-PREDECLARED-PROTOCOL.md:42`). The report preserves the classifications at `STEP-11D3-BR9R2-REPORT.md:845–847`.

**Timing nuance.** The PRE failure is time-specific. The controller's error was stamped at monotonic `134651546019916`; the final embedded observer record later reports `canonicalUncoveredState.attained=true`, starting at `134703028625041` and proving stability at `134759945089333` (85 samples; 56,916,464,292 ns). That later observer interval begins about 51.48 seconds after the controller's failed PRE gate. It did not cause the already failed controller gate to proceed: `finder_actions` remained empty, `measured_phases` empty, and `measured_sequence_completed=false`. Therefore R5/T8 remains supported, but report text saying the certificate was “unattained” should be understood as referring to the failed PRE check, not to every later observer sample. If read as a whole-run claim, it would conflict with the observer's final JSON.

**Gate POST.** `run-result.json` is the hashed primary record: `gate0_post.gate0_status=BLOCKED` and the exact three-element `gate0_blockers` array at lines 65–92, while top-level `status=FAIL` and `is_valid=false`. It captures Gate POST at `14:14:51.607406Z`. The final storage report gives the signed-tree arithmetic and category breakdown at lines 919–931. The separate cleanup receipt's PASS is limited to temporary-prefix cleanup and does not supersede the POST blockers.

## Current surviving signed run-tree inventory

Read-only snapshot of the run directory at review time. Root device/inode: `16777229/98341479`. Inventory contains 20 entries (root, three subdirectories, sixteen regular files), no symlinks. Logical file bytes total 720,231,108; allocated bytes (`st_blocks × 512`) total 725,688,320.

Review inventory digest: `2ed704d0ce598c9cebf4028e9a342cbe174b7427c43aa34bd0253e7c7dbec850`. It is SHA-256 of canonical compact JSONL sorted by relative path; each row binds relative path, kind, mode, device, inode, link count, logical size, allocated block count, and file-content SHA-256. The root row uses path `.`. Files were opened with `O_NOFOLLOW`; pre/post descriptor metadata matched during each read. This is a review snapshot digest, not an existing repository closure certificate or ongoing mutation monitor.

| Relative path | Logical bytes | Allocated bytes | SHA-256 |
|---|---:|---:|---|
| `app-trace.jsonl` | 197,310,536 | 197,312,512 | `d57f5ec94bb2bdab51bebd87c97e3a7bc0650539dd5756897ae39a5aec87774c` |
| `execution-result.json` | 984 | 4,096 | `dca9ab61f70d36a2790243bd2a7b058c7dbda7e09f365f1f89d1f208aab3b4f6` |
| `host-traces/host-trace.58921.jsonl` | 364,651,052 | 369,160,192 | `f839fab2af872f597747a3d1d088e785b3ea4bf8237961e5377c6c49276866d2` |
| `host-traces/host-trace.58942.jsonl` | 1,823 | 4,096 | `03c760a06e261364788571da15264887e5476188829d601bb06b98cd15e7ce5f` |
| `mvk/libMoltenVK.1.dylib` | 10,925,552 | 10,928,128 | `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f` |
| `mvk/libMoltenVK.dylib` | 10,925,552 | 10,928,128 | `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f` |
| `prefix-cleanup.json` | 65,536 | 65,536 | `0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308` |
| `prefix-registry-diff.txt.gz` | 3,755 | 4,096 | `d9ba6648609b3e00d2a79c0be45ef4cf20fbd796fa4cf5c8f02d700344fc1bed` |
| `prefix-state.json` | 2,155 | 4,096 | `6fcc00dd8e06f4c9ad4777a62631697cdfc0fbe075cd21bdcfaefa5d5c3a2c53` |
| `process-context.json` | 7,214 | 8,192 | `154092c5ae04fcd942d1869014c1388e07574fb931f6c6aa5105c6bc78ede2a9` |
| `process-runner.log` | 526 | 4,096 | `140385ceda92b0660cc38e1d0add459ca6d7a26e51cad80336e26ec5c05e6b09` |
| `run-manifest.json` | 23,604 | 24,576 | `e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365` |
| `run-result.json` | 127,112,105 | 127,115,264 | `a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589` |
| `runtime.log` | 1,179,148 | 2,101,248 | `78c918cc0d77bf029349f7a975c9e13f200d56ce2ba94af62a5bb9fa7685f7c3` |
| `trace-index.json` | 7,662 | 8,192 | `1e03ffab47bf76a1d3a440b639b232cf15a9238955e4c64cfd23de2ba3694582` |
| `trace-loader/libMoltenVK.dylib` | 8,013,904 | 8,015,872 | `04bbfc075f6ab999d0538f4a5371f3abd65fb0f0e307be0dea56e47aaefd5157` |

The raw collector/controller/observer hashes above are not included in this signed-tree digest because their saved copies are in the separate `/private/tmp` transient evidence root.

## Uncertainty and evidence limits

- Gate POST's machine record proves `BLOCKED` and the exact blockers, but not the numeric aggregate signed-tree allocation. `598,573,056` comes from the report's preserved reconstruction and category table; the independent current-tree snapshot corroborates it after excluding the post-POST `run-result.json` bytes.
- The unrelated non-lease storage blocker is recorded, but no retained path/inode PRE-versus-POST inventory decomposes it. The 22,900,736-byte difference between the two overrun figures must remain unattributed.
- R5/T8 are exact report classifications, but are not fields in the historical run-result schema. The controller's structured `INVALID_OR_INCOMPLETE` status and empty treatment fields are the underlying machine evidence.
- The controller, raw collector, timeline, and observer evidence currently survive under `/private/tmp`; this review did not move or republish them. Their hashes identify the present files but do not establish indefinite persistence of that external root.
- The current tree digest binds the present snapshot only. This workstream did not test future mutation detection, inspect live process use now, or certify a terminal closure state.
