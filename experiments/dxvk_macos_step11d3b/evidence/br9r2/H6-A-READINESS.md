READY

# H6-A — Environment / Gate Readiness

Checked 2026-10-15. This was a read-only readiness check. I did not run Gate 0, admission, storage smoke, Wine, Finder, or a graphics workload. No source files were changed; this report is the only write from this check.

## Environment and child path

- The current execution environment contains no forbidden startup-hook keys: none matching `PYTHON*`, `LD_*`, `DYLD_*`, or `__XPC_DYLD_*`, and none of `BASH_ENV`, `ENV`, `GCONV_PATH`, `NODE_OPTIONS`, `PERL5OPT`, or `RUBYOPT`. `PYTHONDONTWRITEBYTECODE` is absent.
- `smoke_prefix_lease.py` copies the current process environment, strips its WINE/DXVK/FG/MVK/MTL/VK/DYLD families, then adds only the controlled runtime values at lines 305–328. With the observed clean environment, the failed-attempt hook is not passed to PrefixLease. `_validate_wine_child_environment` still rejects every `PYTHON*`, `LD_*`, and `__XPC_DYLD_*` key, disallowed `DYLD_*` keys, and the named process startup hooks at lines 5010–5020. No guard or whitelist change was found.
- `wine_process_runner.py` sends `context["environment"]` through `lease.set_env` and passes that same environment to `PrefixLease.start_wine_process`; it waits with `PrefixLease.wait_process` (lines 45–73 and 109–115). Current SHA-256 `a6708ef4dd0c2d718419daaa4dda0800781d7da92ef61b8fad60a4b66d0b5f82` matches the accepted H3 fresh-review binding.
- For any Python orchestration that needs bytecode suppression, `sys.dont_write_bytecode = True` remains interpreter-local and does not add an environment variable. Do not place it in the child environment.

## H5 correction-2 and G0C bindings

- Correction-2 is present at `experiments/storage/historical-terminal-closures/step11d3b-baseline-01-20261014-130823-44892b9b.correction-2.json` with SHA-256 `f0317a73c158fadaad52742f13e8944a444fa39e3c6b3a25eb3c3c25389b1613`; its authenticated predecessor SHA is `8201c356ce00f8b2577df90c4e3ed5e6bb229bacef4f1785eac0a6edb5628710` and `authorizes_execution` is false.
- I ran the existing historical-closure verifier directly in read-only mode against the current manifest and output snapshot. It returned **PASS**, `blockers=[]`, allocated bytes `724766720`, and metadata tree SHA-256 `ccfbcba1666d6e252ac3428ff6e7f7b15f22e16382345fe6c0526a6d49b2a6a9`. The verifier also rechecked correction-2, H5 precreation validation, H5-D binding, current control hashes, the closed tree, PrefixLease liveness, and historical process liveness.
- `experiments/storage/g0c-launcher-independent-review.json` reports PASS. All 10 reviewed-file hashes match current file bytes. The H5-D final binding reports PASS; all 14 bound Gate 0 control hashes match, and its correction-2 and precreation-validation hashes match current bytes. Its SHA-256 `a0c3ff84085d6d9a26361b6b93f4df18109a2470a315c590e81fd5a872141902` also matches the digest held by the G0C review.

## H3 runner and replay contract

- The current H3 replay controller is `z9_finder_replay.py`, SHA-256 `162a9ab3a4b0e2013f855f9a0fc0b098536790f4a757057da45f5ae46f1f3f82`. It defines the Visible mask as `1 << 1` (numeric `2`) at line 55. H3-B and H3 fresh review confirm that `8194` sets Visible and `8192` clears it.
- The controller locks the canonical uncovered target before measured PRE (lines 1757–1764), starts an action-free PRE with `finder_actions=[]` and `treatment_window_created=False` (1766–1779), and checks target correlation, uncovered state, renderer health, and visibility throughout the interval (1780–1800). It measures at least 20.000 seconds, with the already-reviewed 250 ms capture tolerance, persists `MEASURED_PRE_COMPLETE`, and only then enables Finder activation; the dedicated window creation follows later (1813–1826, 1839–1871). This matches the accepted H3 action-free PRE ordering.
- The frozen detector currently hashes to `af6c2b69893e6c59fb453f5e140901a711411ba0e75c68747f11eaef1fadd582`, matching the retained controller’s `detector_source_sha256`. `z9_replay_safety.py` hashes to `e42a5391a31d59e82c18ff83658b173f8d3fd82733894b86182a99430b7bf002` and has no working-tree diff. H3-C records the covered predicate, detector thresholds, analyzer classification, and 20/40/20 timings as unchanged. No detector or predicate edit was found.

## Current liveness

- Active PrefixLease marker list: empty. BuildLease `active`, `completed_pending_retirement`, `failed_or_retiring`, and `unknown` lists are empty. The correction-2 liveness verification returned `build_retention_status=QUIESCENT`, zero active prefix markers, historical run processes `58921` and `58942` gone, and no process with the historical output or prefix open.
- `ps` does show two long-lived `winedevice.exe` processes, PIDs `32333` and `32334`. `lsof` identifies them as CrossOver services using `~/Library/Application Support/CrossOver/Bottles/Steam`, not the FG-Metal certified runtime or a PrefixLease path. No FG-Metal runner, target Wine process, or wineserver is active. These unrelated CrossOver services did not change the Gate/H5 liveness result; they were not signaled or modified.

This readiness result does not replace the required fresh read-only admission or authorize execution. The orchestrator must still complete the single new canonical Gate 0 chain and require final PASS with `blockers=[]` before lifecycle authorization.
