# Step 11B evidence

Gate 1 failed visible Wine/DXVK rendering; Gate 2 was not started. See `../REPORT.md`.

- `negative-control-*.json`, `runtime-comparison.json`, `staging-manifest.json`: immutable old hashes and candidate-only payload replacements.
- `build-identity.json`, build logs/settings: exact MoltenVK v1.4.2 source/dependencies, toolchain, deployment target, architecture and binary identity.
- `pipeline-probe.cpp`, SPIR-V sources/binaries/disassembly, `drawindex-old.log`, `drawindex-new.log`: explicit-library x86_64 pipeline A/B. Old expected failure returns 10; new returns 0.
- `teardown-crash-lldb.log`, `teardown-zombie.log`, `interop-probe.mm`, old/new fixed logs: window ownership diagnosis and normal ARC teardown without `_Exit`.
- Per-run directories: original D3D11 test or separate semantics diagnostic, unchanged pinned renderer DLLs, CSVs, native runtime logs, and result JSON. Debug log is deliberately comprehensive; it includes shader dumps and public Metal API Validation.
- `run-summary.json`: parsed application counts, HRESULTs, window flags, renderer-error patterns, and semantics observations. Successful counters do not mean valid pixels.
- `native-clear-control.mm` / log: standalone public Vulkan clear/present isolation. User observed red/green. Its inherited source/synthetic labels do not identify DXGI or DXVK-internal jobs.
- `probe-results.json`: process statuses and direct user visual observations.

Runtime/build scratch is under `/tmp/fgmetal-step11b`; original Step 11A runtime remains under `/tmp/fgmetal-dxvk-macos-audit-run`. Nothing was installed into Highball, Homebrew, system locations or normal game prefixes. No screen capture was used.
