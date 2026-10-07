# Step 11A evidence files

All probe sources and captured logs in this folder were produced for the pinned DXVK-MacOS commit recorded in `../REPORT.md`. The clone and all builds/runtimes stayed under `/tmp`; no renderer source was edited.

- `d3d11-baseline-recheck.log` contains the Wine/MoltenVK capability log and the reproduced D3D11 pipeline failure.
- `d3d11-short-run.csv` is the separate short windowed run. Its `S_OK` rows are not proof of healthy rendering or application invariants.
- `vulkan-probe.mm` and `vulkan-probe.stdout.txt` record the direct x86_64 Vulkan/Metal-object probe. It resolved the MoltenVK dylib from the extracted Wine bundle, but did not run through Wine/winevulkan.
- `google-timing-desired.mm` and `google-timing-desired.stdout.txt` record the isolated FIFO / `VK_GOOGLE_display_timing` run.
- `present-id-wait-fence-chain.mm` and `present-id-wait-fence-chain.stdout.txt` preserve the inconsistent combined-extension experiment; do not use it as timing success evidence.

The MoltenVK dylib SHA-256 and version-report discrepancy are documented in the report. No raw Xcode trace was copied into the workspace because its system display tables could not be attributed to the test surface; the capture remained under `/tmp/fgmetal-dxvk-macos-audit-run/`.
