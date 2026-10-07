# Step 11C.1 — Gate A blocked before implementation

Date: 2026-10-05

## Verdict

**FAIL — the acceptance criteria are unmet, with Gate A blocked. No Step 11C.1 (D) patch or binary was produced. Step 11D remains blocked.**

The required pre-implementation audits found a real worker-owned final-reference path. The frozen DXVK/Wine integration does not establish a non-worker shutdown owner before arbitrary module/process detachment; ordinary COM shutdown can be made explicit inside DXVK, but the required process-detach contract needs a host/runtime handshake. The existing detach guards intentionally avoid blocking cleanup, but ordinary C++ member destruction still follows the early returns. That handshake is needed to close the gap without leaking the device, skipping joins, or risking loader teardown deadlock.

Scheduler changes and Gate C testing were not started because the procedure requires Gate A to pass first. The exact-final-binary visual confirmation is therefore **PENDING**. No screen capture was taken because no candidate binary was launched.

## Frozen input identity

- DXVK base: `8d348236e14a3db25ffbe528a83010b3dd69a3ef`
- Step 11B.1 prerequisite SHA-256: `4f4973000d0ce0783d05e11b7acae0efdff2d6bfe898bf49305408d72280e758` (reverified)
- Step 11C experiment SHA-256: `4df3af55c9491a44d5e8cec79d458bf7cecc18d71b61312b19e80b461023d352` (reverified)
- MoltenVK and Wine remain the frozen v1.4.2 / MetalSharp Wine 11.17 inputs.
- D patch/hash: **not produced**. The original FG-Metal checkout remains at `37d98f62a733f2048785af2da84b7a1746acd919`; its pre-existing changes were preserved.

The read-only staged B+C source used for inspection is at `/tmp/fgmetal-step11c1-candidate`.

## Gate A — ownership proof and blocker

The source establishes this ownership path:

1. `DxvkDevice` contains its `DxvkSubmissionQueue` as a direct member (`src/dxvk/dxvk_device.h:791`).
2. A `Presenter` retains `Rc<DxvkDevice>` (`src/dxvk/dxvk_presenter.h:436`).
3. Internal requests and queued present entries retain `Rc<Presenter>` (`src/dxvk/dxvk_queue.h:49-64,82-90`). The submit and finish workers move these entries into worker-local state before processing/retirement (`src/dxvk/dxvk_queue.cpp:228-285,747-795`).
4. If releasing such a worker-local entry drops the last `Presenter`/`DxvkDevice` reference, `DxvkDevice::~DxvkDevice` begins on that worker. Its queue member is then destroyed as part of normal member destruction.
5. `DxvkSubmissionQueue::~DxvkSubmissionQueue` sets stop, wakes workers, and unconditionally joins submit and finish threads (`src/dxvk/dxvk_queue.cpp:66-87`). `thread::join` explicitly throws `resource_deadlock_would_occur` when asked to join the current thread (`src/util/thread.cpp:29-39`). Since this occurs during destruction, an escaping self-join exception terminates the process.
6. The module-detach early return in `DxvkDevice::~DxvkDevice` skips the destructor body’s `waitForIdle` and pipeline-worker stop, but cannot skip automatic member destruction (`src/dxvk/dxvk_device.cpp:55-69`). D3D11/D3D9 detach early returns have the same member-lifetime limitation (`src/d3d11/d3d11_swapchain.cpp:85-95`, `src/d3d11/d3d11_context_imm.cpp:43-52`, `src/d3d9/d3d9_swapchain.cpp:50-72`, `src/d3d9/d3d9_device.cpp:157-173`).

This is a proven possible reference path, not a reproduced crash. The existing Step 11C runner points to the removed `/tmp/fgmetal-step11c/build` tree and the stale `/tmp/fgmetal-step11b1/runtime-frozen` Wine path. The only local D3D11 DLL found is `/tmp/fgmetal-step11b/app/d3d11.dll` (SHA-256 `6940b883fb4bc1b744fb7ca3aa6a486303eaf180c9fe85c03ed7cb175057bf28`), not the frozen Step 11C DLL (`3c39a706d2fb577825592b9bfbc335feb218f93f933e7b83a25dcda8e1f32c3c`). I did not use that different binary as shutdown evidence. No diagnostic binary or deterministic last-reference injection was built, and the 100-iteration matrix was not run because there is no shutdown-owner fix to test yet.

`RtlDllShutdownInProgress` is queried only in `src/util/thread.cpp:82-89`; the DXVK source has no pre-detach callback or owner handshake. The frozen environment supplies a Wine runtime binary, not a Wine source tree or a host hook that can call DXVK while a non-worker owner is still available. Joining from DLL teardown risks the loader/shutdown deadlock that the early returns are intended to avoid. Omitting the joins or retaining/leaking the queue/device would violate the stated requirements. Therefore Gate A cannot be claimed closed with the available DXVK-only integration.

A safe ordinary COM teardown path appears implementable in DXVK: the D3D11/D3D9 frontend owner can retain its `Rc<DxvkDevice>` and call an idempotent shutdown before releasing it. That operation must first stop producers and close admission, then drain active submissions, finish entries and WSI leases, wait for GPU work, stop and join both workers from that non-worker owner, and only then release Presenter/Device ownership. `waitForIdle()` by itself does not close admission. This would protect normal teardown but does not prove arbitrary process-detach behavior.

The current shutdown implementation has additional unclosed paths: `processInternalPresent` has no stop checks before proceeding through Vulkan work; the finish worker returns as soon as `m_stopped` is set even if the completion queue still contains entries; and the queue destructor drops armed requests while holding `m_mutex`, so releasing the last Presenter there can re-enter queue/device cleanup while the mutex is held. These need deterministic shutdown tests after an owner-side shutdown contract exists.

## Read-only subagent findings

- **Scheduler timeline / performance:** historical 15 Hz data is 301 SOURCE WSI, 42 INTERNAL WSI, 343 total; drops were 155 `queue-full`, 97 `deadline-expired`, 4 `deadline-expired-before-submit`, and 3 `source-pending`. The raw log shows earlier opportunities remain armed while later ones hit `queue-full`, but does not contain per-request timestamps or enough state to compute residence-time buckets. B2/B3 are not quantitatively closed.
- **Presenter acquire / source priority:** SOURCE work is selected before the internal slot. Internal acquire is nonblocking and uses cached WSI state; it does not use the source blocking-acquire path. The audit found the wait implementation is a likely dominant cause before any slot redesign: the Windows `condition_variable::wait_until` passes `now - time` for a future deadline (`src/util/thread.h:311-325`), producing a negative timeout that is passed to `SleepConditionVariableSRW` as a `DWORD` (`:328-335`). Source queue notifications can wake the worker, but source work is prioritized and the armed opportunity can persist. This explains the observed shape but is not yet a timestamp-instrumented B2/B3 proof. No scheduler tuning was applied.
- **Optional slot:** do not replace the FIFO solely to treat the symptom. First correct and measure timed wake behavior. If stale ARMED occupancy remains, use one replaceable optional ARMED slot, transfer a claimed request to worker-local ownership, and terminalize every superseded opportunity exactly once.
- **Lifecycle:** resize/recreation locking and generation guards have useful structure, but `D3D11SwapChain::ChangeProperties` currently calls `setSurfaceExtent` with the old `m_desc` dimensions before assigning `pDesc` (`src/d3d11/d3d11_swapchain.cpp:191-195`). Runtime resize/fullscreen/minimize evidence is absent.
- **Failure matrix:** no deterministic injection hooks exist. An internal submit error can update the shared `m_lastError` path and suppress subsequent SOURCE submissions. Shutdown also needs explicit treatment of active internal recording, queued completion entries, and worker exit with pending finish entries. None of these issues was changed or runtime-tested.
- **Adversarial final review:** not run because there is no implementation or exact final binary to review.

## Gate status

| Gate | Result | Evidence boundary |
|---|---|---|
| A — shutdown/lifetime | **BLOCKED** | Static ownership path proven; no fix, runtime repro, or stress result |
| B — cadence | **NOT STARTED** | Existing C run remains 42 INTERNAL / 301 SOURCE at 15 Hz; no D timeline |
| C — lifecycle/failure | **NOT STARTED** | No D binary or injected lifecycle/failure runs |
| Exact-binary visual | **PENDING** | No D binary launched; no direct human observation |
| Step 11D | **BLOCKED** | Full PASS criteria are unmet |

## Disk and temporary files

At the end of this audit, `/` had 24 GiB available (95% capacity). No build or test outputs were generated. The 9 MiB staged candidate checkout, 201 MiB clean source checkout, and 6.6 GiB Wine/MoltenVK runtime are still usable inputs for resuming the work, so they were preserved. No non-usable task-generated temporary files were found to remove.
