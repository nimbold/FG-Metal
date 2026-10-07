# MoltenVK / SPIRV-Cross DrawIndex audit

Audit date: 2026-10-04. This is a source and release-history audit using KhronosGroup's upstream MoltenVK and SPIRV-Cross repositories. No runtime or source was modified for this audit.

## Finding

The Step 11A diagnostic is the expected behavior of the SPIRV-Cross MSL backend pinned by MoltenVK 1.4.1. MoltenVK v1.4.1 pins SPIRV-Cross `adec7acbf41a988713cdb85f93f26c8ca5ea863e`; that revision explicitly throws `DrawIndex is not supported in MSL.` for `BuiltInDrawIndex` translation. The message matches the reported Step 11A failure.

The fix landed in SPIRV-Cross pull request [#2634](https://github.com/KhronosGroup/SPIRV-Cross/pull/2634), merged May 20, 2026. Its merge commit is `38681a30e09679191cc3957719eeee76024f6daf`. It removes the MSL rejection and emulates DrawIndex by adding a constant draw-ID buffer to generated MSL, then reading that buffer where the shader uses `BuiltInDrawIndex`. The change includes MSL output-reference tests for ordinary vertex and tessellation paths. Since the feature is emulated, the final PR revision removed MSL-version and device checks.

MoltenVK v1.4.2 pins SPIRV-Cross `6c09849fe88c48eaed08413aa022aaa136a3a057`, which is after and includes the DrawIndex change. The v1.4.2 release notes explicitly list “Add gl_DrawID / DrawIndex support for MacOS,” “Improve handling of draw ID buffer binding,” and the SPIRV-Cross MSL support. Therefore, v1.4.2 is the minimum upstream release that is source-level sufficient to remove this exact known blocker. That is a strong, testable hypothesis for the baseline repair, not experimental proof that the runtime or DXVK lane will otherwise be healthy.

## Revisions and evidence

| Component | Pinned revision | Date / status | DrawIndex evidence |
|---|---|---|---|
| MoltenVK v1.4.1 | `db445ff2042d9ce348c439ad8451112f354b8d2a` | Released Nov 30, 2025 | Pins SPIRV-Cross `adec7acbf41a988713cdb85f93f26c8ca5ea863e`; that source contains the MSL throw. |
| SPIRV-Cross support | `38681a30e09679191cc3957719eeee76024f6daf` | Merged May 20, 2026 | Implements `gl_DrawID` using an emulated buffer and adds output tests. |
| MoltenVK v1.4.2 | `db66022459ffb663aa2b50f6b018bc2e124f5edf` | Released Jul 24, 2026 (release page); release commit dated Jul 23 | Pins SPIRV-Cross `6c09849fe88c48eaed08413aa022aaa136a3a057`; release notes list macOS DrawIndex support. |
| MoltenVK upstream `main` snapshot | `52aa21f54d7a84c5c441fc26359692b0980b384c` | `git ls-remote` snapshot on audit date | Its `ExternalRevisions/SPIRV-Cross_repo_revision` is still `6c09849fe88c48eaed08413aa022aaa136a3a057`. |

The official releases page identified v1.4.2 as the latest release, and the upstream tag listing returned no `v1.4.3` tag at audit time. Thus, “MoltenVK 1.4.3” in separate runtime metadata does not match an upstream release tag found in this audit. This does not identify which runtime file supplied that string or explain the Step 11A metadata conflict; runtime identity remains a separate experimental question.

Primary-source links:

- [MoltenVK v1.4.1 release](https://github.com/KhronosGroup/MoltenVK/releases/tag/v1.4.1)
- [MoltenVK v1.4.1 SPIRV-Cross revision pin](https://raw.githubusercontent.com/KhronosGroup/MoltenVK/v1.4.1/ExternalRevisions/SPIRV-Cross_repo_revision)
- [SPIRV-Cross v1.4.1-pinned MSL source](https://github.com/KhronosGroup/SPIRV-Cross/blob/adec7acbf41a988713cdb85f93f26c8ca5ea863e/spirv_msl.cpp)
- [SPIRV-Cross PR #2634](https://github.com/KhronosGroup/SPIRV-Cross/pull/2634)
- [SPIRV-Cross DrawIndex support commit and diff](https://github.com/KhronosGroup/SPIRV-Cross/commit/38681a30e09679191cc3957719eeee76024f6daf)
- [MoltenVK v1.4.2 release](https://github.com/KhronosGroup/MoltenVK/releases/tag/v1.4.2)
- [MoltenVK v1.4.2 SPIRV-Cross revision pin](https://raw.githubusercontent.com/KhronosGroup/MoltenVK/v1.4.2/ExternalRevisions/SPIRV-Cross_repo_revision)
- [SPIRV-Cross revision bundled by MoltenVK v1.4.2](https://github.com/KhronosGroup/SPIRV-Cross/tree/6c09849fe88c48eaed08413aa022aaa136a3a057)
- [MoltenVK current `main` SPIRV-Cross revision pin](https://raw.githubusercontent.com/KhronosGroup/MoltenVK/main/ExternalRevisions/SPIRV-Cross_repo_revision)

## Classification

- **Verified from upstream source:** v1.4.1's pinned MSL compiler rejects `BuiltInDrawIndex`; the Step 11A message is that exact rejection. SPIRV-Cross added an emulated `gl_DrawID` MSL path in commit `38681a3…`; the revision pinned by MoltenVK v1.4.2 contains it. MoltenVK v1.4.2 lists macOS DrawIndex support in its release notes.
- **Supported inference:** the v1.4.1 translator rejection is the root cause of the reported shader-conversion error, and v1.4.2 should clear this exact blocker without patching DXVK's shader.
- **Not tested by this audit:** direct Vulkan old/new pipeline A/B, the loaded MoltenVK binary's true revision, the full DXVK baseline, or any claim that DrawIndex is the only remaining rendering problem.
