# Step 11B Subagent 4 — Runtime upgrade isolation and safety

Audit scope: read-only review of the Step 11B report, staging and negative-control manifests, current runtime hashes, MoltenVK source/build identity, Wine runner, and captured loader logs. No runtime, Highball, system Wine, Homebrew, or game-prefix file was changed by this audit.

## Finding

**VERIFIED WITH SCOPE LIMITS:** the 1.4.2 upgrade was staged into a separate candidate Wine tree and exercised with a separate `/tmp` prefix. The original runtime at `/tmp/fgmetal-dxvk-macos-audit-run/runtime/wine` still matches all 25 records in the pre-stage negative-control manifest. The archived negative-control payloads also independently match all 25 recorded hashes. The experiment is isolated enough to protect the original runtime from this candidate swap. This does not establish a shippable or visually correct Wine/DXVK upgrade: the user reports the Wine/DXVK window is black, so Gate 1 remains **FAIL** and Gate 2 remains **NOT TESTED**.

The source pin is MoltenVK `db66022459ffb663aa2b50f6b018bc2e124f5edf` (`v1.4.2`); the checkout is clean. The built universal dylib has SHA-256 `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e`, with x86_64 and arm64 slices. The old x86_64 Wine provider was `libvulkan.dylib`, hash `1d57a14b4a0d380420ab566884018fd83ba57381aa44e464f79472a77273b6bc`, reporting MoltenVK 1.4.1 / Vulkan 1.4.334. The old separate `libMoltenVK.1.dylib` payloads were 1.4.3, hash `8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf`.

Three regular payload files were replaced in the candidate tree; each now has the built 1.4.2 hash above:

| Candidate path | Before SHA-256 | After SHA-256 |
|---|---|---|
| `lib/moltenvk-vkmt/libMoltenVK.1.dylib` | `8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf` | `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e` |
| `lib/wine/x86_64-unix/libvulkan.dylib` | `1d57a14b4a0d380420ab566884018fd83ba57381aa44e464f79472a77273b6bc` | `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e` |
| `lib/wine/x86_64-unix/libMoltenVK.1.dylib` | `8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf` | `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e` |

`runtime-comparison.json` reports five changed path records because it follows two symlink aliases (`libvulkan.1.dylib` and `libMoltenVK.dylib`) into those replacement payloads. They are not two additional files written. The old and candidate runtime roots resolve to different directories and their checked payload files have distinct inodes and link count 1. The report records an APFS copy-on-write candidate; the current path/inode evidence confirms separation, while the original hashes confirm its contents remained intact.

## Provider and ABI evidence

The runner names the candidate Wine executable explicitly and sets `WINEPREFIX` under `/tmp/fgmetal-step11b`; the old-runtime replay uses the original Wine executable with its own `/tmp/fgmetal-step11b/prefix-negative`. DXVK DLLs and logs are kept in the experiment evidence directories. It does not select an installed Wine executable, Highball bundle, Homebrew prefix, or a game-owned prefix. The old runtime's current hashes remain 25/25 equal to baseline.

`DYLD_PRINT_LIBRARIES` captures Wine loading the exact candidate path `.../runtime-candidate/wine/lib/wine/x86_64-unix/libvulkan.dylib`; its startup identifies MoltenVK 1.4.2 / Vulkan 1.4.357. The Wine Vulkan enumeration reports `driverInfo=1.4.2`. The candidate dylib has x86_64 and arm64 slices and depends on Apple system frameworks and libraries. Its `LC_ID_DYLIB` is `@rpath/libMoltenVK.dylib`, whereas the old provider's install name is `@rpath/libMoltenVK.1.dylib`. Wine loads the candidate by the explicit `libvulkan.dylib` path, and the captured x86_64 runs exercise initialization, device creation, shader pipeline creation, and repeated WSI Present calls. This supports ABI compatibility for the exercised Wine/DXVK path; it is not a general ABI certification for untested Wine APIs or an arm64 Wine process.

The user confirms the separate native Vulkan clear control alternates red and green on the same 1.4.2 provider. That verifies native clear/present output, not the Wine/DXVK surface path. The candidate Wine/DXVK D3D11 window remains black. The old-runtime replay now also captures the original `libvulkan.dylib` provider path and reproduces `DrawIndex is not supported in MSL`; that replay was watchdog-terminated after 55 seconds, so its overall process result is a timeout rather than a clean exit.

## Isolation limits and gate

The 25-record negative-control manifest covers the tested DXVK DLLs and selected Wine runtime/provider/ICD files. It does not fingerprint every file in Highball, Homebrew, system locations, or the user's game prefixes. Their non-use is supported by the reviewed runner's explicit `/tmp` executable and prefix paths and the recorded build flow (`fetchDependencies --macos`, `make macos`), rather than global before/after hashes. No reviewed command targets those external locations; a global contents audit was not performed.

The old runtime, candidate staging, source pin, and tested provider identity are sufficiently isolated for this experiment. The visual Gate 1 failure is independent and controlling: no downstream DXVK WSI/presentation implementation or Gate 2 test is authorized by this result. `REPORT.md` correctly keeps Gate 2 untested and RIFE blocked.

Evidence: [main report](../REPORT.md), [staging manifest](../evidence/staging-manifest.json), [runtime comparison](../evidence/runtime-comparison.json), [negative-control manifest](../evidence/negative-control-manifest.json), [after-test hashes](../evidence/negative-control-after.json), [archived payloads](../evidence/negative-control-payloads.tar.gz), [build identity](../evidence/build-identity.json), [runner](../evidence/run-baseline.py), [candidate Wine log](../evidence/lifecycle-65s/runtime.log), [old-runtime replay](../evidence/negative-old-hud-10s/runtime.log), and [native clear log](../evidence/native-clear.log).
