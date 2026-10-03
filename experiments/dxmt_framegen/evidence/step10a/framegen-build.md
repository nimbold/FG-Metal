# STEP 10A.1 Framegen baseline evidence

Captured: 2026-10-03 00:19:19 Asia/Tehran

## Commands and results

```sh
cmake -S . -B build/step10a-agent-framegen -G Ninja
cmake --build build/step10a-agent-framegen --verbose
ctest --test-dir build/step10a-agent-framegen --output-on-failure
ctest --test-dir build/step10a-agent-framegen -V -R '^framegen-metal-c-api-tests$'
```

Configure and build exited 0 with no warnings or errors. The full CTest run exited 0 (5/5 passed):

| Test | Result | Duration |
| --- | --- | ---: |
| `framegen-tests` | Passed | 0.92 s |
| `framegen-pacing-host` | Passed | 0.35 s |
| `framegen-c-api-tests` | Passed | 0.37 s |
| `framegen-c-api-header-smoke` | Passed | 0.35 s |
| `framegen-metal-c-api-tests` | Passed | 2.58 s |

The verbose Metal-only CTest rerun also passed (0.73 s); its command was the build-local `tests/framegen-metal-c-api-tests`. CTest set `FRAMEGEN_MODEL_WEIGHTS` to `/Users/<user>/Documents/Code/FG-Metal/models/local/rife-v4.26/rife-v4.26.fgweights`, matching the test configuration in `tests/CMakeLists.txt`. The test did not skip (the configured skip return code is 77). No model weights were copied or changed.

## Metal compilation evidence

The build compiled and archived the Objective-C++ Metal backend and generated its embedded shader header. The integration test then exercised runtime backend creation with the configured weights. `src/metal/metal_backend.mm` creates the RIFE library using `newLibraryWithSource`, creates compute pipelines from shader source, and the model code compiles MPSGraph programs. The Metal C API integration test passed, establishing success for these runtime compilation paths in this run. CMake does not define a separate offline `.metal` compiler target.

## Toolchain and resource snapshot

- Host: macOS 27.0.1, arm64; Xcode developer directory `/Applications/Xcode.app/Contents/Developer`.
- AppleClang 21.0.0.21000334; CMake 4.4.3; Ninja 1.13.2.
- SDK: `/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk`.
- Configure defaults enabled `FRAMEGEN_BUILD_LIBRARY`, `FRAMEGEN_BUILD_METAL`, `FRAMEGEN_BUILD_TESTS`, and `FRAMEGEN_BUILD_TEST_METAL`.
- Build directory size: 12 MB (`du -sh` reported `12M`). It is ignored by `.gitignore` (`/build/`).
- Disk snapshot (`df -h .`): 460 GiB total, 372 GiB used, 62 GiB available, 86% capacity.

The working tree's pre-existing edits were left untouched; this note is under the ignored build directory and no tracked source was modified.
