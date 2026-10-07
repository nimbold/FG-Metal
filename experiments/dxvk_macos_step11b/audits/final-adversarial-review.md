# Step 11B final adversarial review

Scope: final read-only check of `REPORT.md`, `audits/adversarial.md`, `evidence/run-summary.json`, `evidence/probe-results.json`, and the relevant run logs and manifests. No source or runtime files were changed.

## Adjudication

**Gate 1 FAIL is supported. Gate 2 remains NOT TESTED.** The recorded user observations say the Wine/DXVK window is black and the separate native clear-control window renders red and green; the user's latest reply confirms the latter. A successful native Vulkan control does not establish that Wine/DXVK displays its sampled blit correctly.

The 30/60-second counts are application-side `Present` calls from the clear-window diagnostic. The 60-second app CSV has 1,800 calls, with visible-window flags and selected clear-color IDs, but none of those fields prove pixels reached or appeared in the window. The diagnostic app also toggles its own `FG_AUDIT_ENABLE` environment value and logs `framegen_enabled`/`framegen_disabled`; these are app-side labels and do not activate generated output in the unchanged renderer. The waitable diagnostic sets maximum frame latency to 1 and polls its handle after each `Present`; the report records these app-visible changes and correctly limits the result to observations. The lifecycle run records 1,921 `S_OK` and 30 `DXGI_STATUS_OCCLUDED` results while minimized, plus resize, minimize/restore, fullscreen, and swapchain recreation. These API effects do not turn the black result into a pass.

The native log's 1,800 successful WSI calls belong to a standalone process. Its `source`/`synthetic` labels describe that control's clear colors; logged `desired_ns` proposals were not submitted as desired-present times. Neither its 60 timing records nor its successful presents establish app-facing invariants, integrated cadence, or Gate 2 behavior. The report makes this distinction and says no internal present or bookkeeping split was implemented.

## Correction to the prior audit

The prior adversarial audit says the current `run-baseline.py` sets `DXVK_HUD='fps'` unconditionally. That sentence is stale: the saved runner now accepts `--no-hud` and records the effective environment in a newly written `result.json`. The archived per-run result files have no `environment` field, however, so their historical HUD settings remain unverified. The report's caveat that HUD attribution is not used as A/B proof is appropriate; this correction does not affect the direct DrawIndex probe or Gate 1 result.

## Final boundary

The report consistently treats API success and native colored output as scoped observations, preserves the user's black Wine/DXVK observation as the Gate 1 failure, and leaves Gate 2 untested. No count or native result found here implies successful internal presents or proves the required healthy pre-G baseline.
