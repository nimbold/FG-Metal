# Step 11C.1R — Shutdown contract and gated re-evaluation

**Result: PARTIAL PASS. Full Gate A is blocked, so Gate B and Gate C were not started. Step 11D remains blocked.**

This report covers the reconstructed Step 11B.1 + 11C candidate, the D1 normal-shutdown/process-termination work, the independent D4 resize correction, and the evidence collected before the D3D9 build blocker. It does not claim full Windows validation: runtime tests used the frozen MetalSharp Wine 11.17 package with MoltenVK 1.4.2.

## Windows shutdown contract

- **Normal COM/device teardown while the process remains alive:** the frontend device owner must explicitly close admission, drain accepted renderer work, retire GPU/WSI work, stop and join workers from a non-worker owner, then release Presenter and Device ownership. This is where deterministic cleanup is required.
- **`FreeLibrary`:** an unload reaching zero references invokes `DLL_PROCESS_DETACH`. Renderer objects and code-executing workers must already have been shut down before the DLL is unloaded. The controlled unload test below follows that order.
- **`ExitProcess` / normal program return:** Windows terminates peer threads before DLL process-detach notifications. Waiting for those threads or doing complex cleanup under loader detach can deadlock or access inconsistent state. DXVK therefore retains the process root and does not run normal renderer shutdown in this path.
- **`TerminateProcess`:** no DLL detach callback or renderer cleanup is promised. The design relies on OS process reclamation, not a callback.

These distinctions follow Microsoft's descriptions of [`DllMain`](https://learn.microsoft.com/en-us/windows/win32/dlls/dllmain), [`ExitProcess`](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-exitprocess), [`FreeLibrary`](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-freelibrary), and [`TerminateProcess`](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess). In particular, Microsoft warns against complex process-detach cleanup and documents that `TerminateProcess` does not send `DLL_PROCESS_DETACH`.

## D1 implementation and ownership

The D1 patch adds an idempotent queue/device shutdown path with the conceptual progression `RUNNING → CLOSING_ADMISSION → DRAINING → STOPPING_WORKERS → STOPPED`.

- Admission closes before the drain. New internal work is rejected after that boundary; frontend source submissions that reach a closing device are rejected by the defined shutdown path.
- Internal-present shutdown checks guard WSI acquisition, recording, submission, and present. Already-submitted work continues through normal retirement so an acquired WSI lease is not abandoned.
- Accepted finish-worker entries are retired before worker exit. The stop request and drained-queue condition are separate; the finish worker does not exit with accepted completion work stranded.
- `shutdownAndJoin()` is owned by the D3D11 or D3D9 frontend device teardown path. It asserts it is not running on either queue worker, drains and waits required GPU work, requests worker stop, joins both workers, then marks the queue stopped.
- Queue-owned request and Presenter references that could be final references are moved out of queue/lifetime mutexes before release. Device/Queue destruction cannot re-enter while those locks are held.
- The Windows-only process-lifetime registry is allocated without automatic static destruction and strongly roots live `DxvkDevice` objects and relevant Presenter ownership. Normal shutdown unregisters only after queues and Presenter workers have stopped. Process termination intentionally leaves the roots in place for OS reclamation; this is not normal-runtime retention.
- On process termination, detach paths return without invoking normal shutdown, joining workers, waiting for GPU work, coordinating workers, unregistering the anchor, or destroying rooted Device/Queue objects.
- Existing `RtlDllShutdownInProgress()` use is retained as the process-termination indicator, with a null check. Device/worker construction primes the helper before detach can occur. Microsoft documents this helper for Windows 10 and later at [`RtlDllShutdownInProgress`](https://learn.microsoft.com/en-us/windows/win32/devnotes/rtldllshutdowninprogress); native-Windows availability and behavior were not runtime-tested here.
- The D3D9 CS-thread shutdown now joins the CS worker and releases its retained context before unregistering the Device root, so context-owned descriptor work is stopped while Device ownership remains anchored.
- The thread-wrapper audit confirmed that `detach()` alone does not protect worker access to its owning queue. Ordinary queue destruction is reached only after worker wrappers are joined and non-joinable; process termination instead keeps the Device/Queue root alive until OS reclamation.

The implementation is in [D1-shutdown-lifetime.patch](patches/D1-shutdown-lifetime.patch). It changes queue, device, presenter, process-lifetime registry, frontend teardown, adapter/fence/cache/descriptor worker paths, and thread helper code. D3D9 source-side ordering is included, but its DLL could not be built against the frozen dependency (see blocker below).

## Gate A evidence

All successful runtime cases below used the exact D3D11/DXGI binaries identified later in this report, with INTERNAL enabled where applicable. Detailed raw evidence is in the linked JSON files.

| Case | Result | Evidence |
|---|---:|---|
| 100 normal create/present/release cycles | 100/100 exited successfully; INTERNAL enabled; a joinable descriptor worker was test-forced in each cycle. Each teardown recorded zero Device/Presenter roots and both queue workers joined. | [normal stress](evidence/d1-normal-stress-descriptor-worker.json) |
| Normal shutdown by active state | 9/9 clean: empty, armed, claimed, image-acquired, command-recording, submission-queued, GPU-pending, present-pending, retirement-pending. Stage gates were reached; Presenter lease/pending counters were clear and queue workers joined. | [state matrix](evidence/d1-shutdown-stage-matrix.json) |
| Process termination at timing offsets | 12/12 zero exit status at 0, 1, 5, 10, 17, 20, 25, 33, 50, 100, 250, and 500 ms. Every case showed source presentation, zero process-root destruction, and no normal Device-shutdown log. | [timing matrix](evidence/d1-process-exit-12-offsets.json) |
| Process termination during active INTERNAL states | 8/8 zero exit status across armed through retirement-pending states; each reached its active stage and avoided normal Device shutdown. | [active termination](evidence/d1-active-process-exit-8-stages.json) |
| Descriptor-worker detach probe | Worker-start handshake and active-stage gate passed; the helper asserted its detach marker; no normal Device shutdown ran. This supports the intended process-detach branch. A negative-control run was not performed. | [detach probe](evidence/d1-descriptor-worker-detach.json) |
| Controlled dynamic unload | Explicit object/worker teardown followed by `FreeLibrary` completed; `released=1`, `freelibrary=1`, `still_loaded=0`; workers were joined and roots were zero before unload. | [dynamic unload](evidence/d1-dynamic-unload.json) |

`TerminateProcess` itself was not exercised; no cleanup callback is part of the contract. The process-exit evidence is from Wine and is not native Windows evidence.

## Gate A blocker: D3D9 build

The D3D9 DLL build fails against the frozen `dxbc-spirv` pin `bf14419e5fa7eacb817b7b632f03cb61d61bbad7`: candidate source `src/d3d9/d3d9_shader.cpp:1121` assigns `dxbc_spv::sm3::Converter::Options::deAliasedSamplers`, but that member is absent from the pinned dependency. Ninja compiled several D3D9 objects, then stopped at this compile error. The D3D9 DLL did not link, so D3D9 normal teardown, stress, and process-termination behavior have no runtime evidence. The frozen submodule was left unchanged and no compatibility fallback was added. See [D3D9 build log](evidence/d3d9-build-failure.log).

Because the requested shutdown gate covers both frontend owners and D3D9 runtime teardown could not be exercised, **Gate A does not pass** despite the D3D11 matrix. Work stopped here rather than proceeding to Gate B.

## D2 timed-wait audit (read-only; not implemented)

The Windows `dxvk::condition_variable` implementation in `src/util/thread.h` has a confirmed `wait_until` sign error in both overloads: while `now < time`, it computes `now - time` instead of the positive remaining interval `time - now`. That can turn a future deadline into an already-expired/zero timeout and disrupt the internal midpoint scheduler.

The `wait_for` conversion also needs more than the sign correction: direct millisecond casts truncate positive sub-millisecond waits to zero; negative signed durations can become very large `DWORD` values or collide with `INFINITE`; large positive durations can overflow. Predicate waits currently return after a wake and predicate check rather than re-waiting until the predicate is true or the deadline expires.

The complete Windows call-site audit found:

- `src/dxvk/dxvk_queue.cpp:420` — internal submit scheduler `wait_until` deadline; likely directly affects INTERNAL opportunity timing.
- `src/dxgi/dxgi_adapter.cpp:541` — 1500 ms predicate `wait_for` used by adapter budget polling; its positive duration conversion is in range, but spurious/false wakes can return early.

No D2 code or regression tests were run because Gate A did not pass. The intended fix needs a nonnegative remaining duration, zero for expired deadlines, positive-duration round-up to at least 1 ms, safe finite clamping below the `INFINITE` sentinel, and predicate re-checking through the deadline. Future/past deadlines, short and large waits, timeout, notification, and spurious wake behavior remain unverified.

## Scheduler and remaining gates

No post-fix 15 Hz scheduler run exists because D2 was not applied. The scheduler review's prediction (not measurement) is that correcting future waits could restore roughly one INTERNAL midpoint opportunity per 15 Hz source interval, approaching 30 total WSI/s if source pacing, queue availability, and GPU/present retirement permit it. There are no request timestamps or measured counts for SOURCE, INTERNAL, queue-full, deadline-expired, source-pending, or total WSI/s after a wait repair. Scheduler capacity, midpoint formula, deadline policy, and source priority were not redesigned.

Accordingly, 15→30 and 30→60 were not run. No final scheduler binary or exact-binary user visual confirmation exists. Gate C resize/fullscreen/minimize/failure/flip-model/soak work was not started. The separate D4 resize correction below was not hidden in scheduler changes. INTERNAL failure isolation from shared `m_lastError` was not implemented or tested. The Step 11B.1 baseline was not revalidated in this run.

## Independent D4 resize correction

The audit confirmed `D3D11SwapChain::ChangeProperties` updated Presenter surface extent using `m_desc.Width/Height` before assigning `*pDesc`, so it used the old dimensions. D4 assigns the new description before updating extent and recreating buffers. The structural order check passed. Visible and runtime resize regression coverage is deferred to Gate C and has not been performed.

See [D4-resize-extent.patch](patches/D4-resize-extent.patch).

## Exact inputs, patches, and binaries

| Artifact | Identity |
|---|---|
| DXVK base | `8d348236e14a3db25ffbe528a83010b3dd69a3ef` |
| Step 11B.1 input patch SHA-256 | `4f4973000dce0783d05e11b7acae0efdff2d6bfe898bf49305408d72280e758` |
| Step 11C input patch SHA-256 | `4df3af55c9491a44d5e8cec79d458bf7cecc18d71b61312b19e80b461023d352` |
| Reconstructed B+C patch SHA-256 | `9f6aa65b3a7fed0b16ed9b2fa50b8361d7ac89c00f52629ccf819867360b0ea2` |
| D1 shutdown/lifetime patch SHA-256 | `a8a9d41b562ae79eae9ee82e3aa1da97562c28a6e6f0c08c7419687d2730b46b` |
| D4 resize patch SHA-256 | `07b460c16b6c7ae670658be2ed24071e4127835543ac4983ae96dcac5170d7b8` |
| D3D11 DLL SHA-256 (Gate A tests) | `5f41dc59a12b492530781daf85c2be35c7cabb4cc88e4384b61353b7d75b6956` |
| DXGI DLL SHA-256 (Gate A tests) | `0319f3ddca7d1a6cd18aaa528df1ac95380d72b8d68002848b7a524282d340c2` |
| MoltenVK | v1.4.2, `db66022459ffb663aa2b50f6b018bc2e124f5edf`; loader override SHA-256 `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f` |
| Wine | MetalSharp 0.70 bundled Wine 11.17; launcher SHA-256 `946a7032484d02981182d59f82edda03f40f38c510e0208d48482c0830ef20e9` |

The D3D11 and DXGI hashes identify the exact pair exercised by the Gate A tests; those binaries contain both D1 and the separate D4 source change. No D3D9 binary was produced. The reconstructed inputs and patch identities are also recorded in the [identity manifest](evidence/identity-manifest.json).

## Review and final disposition

The independent reviews challenged termination retention, normal unload cleanup, worker ownership, stop/drain ordering, reference drops under mutexes, timed-wait semantics, scheduler implications, and thread-wrapper behavior. The concrete D3D9 CS-context/descriptor-worker ordering issue was corrected in D1 source. The descriptor detach probe was strengthened with a unique marker and worker-start handshake; its negative control remains absent. No reviewer evidence substitutes for the D3D9 build/runtime gap or native-Windows validation.

**Gate A: PARTIAL PASS (D3D11 evidence passes; D3D9 build/runtime blocks the gate). Gate B: not started. Gate C: not started. Overall: PARTIAL PASS.**

STEP 11D STILL BLOCKED.
