# Step 10B DXMT cross-build evidence

**Source:** unmodified downstream worktree at DXMT commit `fb4515681daefb789a4d0f403c4bdbca88f3b3de`.

The earlier native arm64 D3D12 build attempt failed because the host DirectX-Headers do not declare interfaces already referenced by this DXMT revision, including `ID3D12Fence1` and `ID3D12Device4`. I retried through DXMT's supported Windows cross-build file instead of treating that native-build failure as a blocker.

## Configuration and result

Meson configured an x86_64 Windows cross build using the repository's `build-win64.txt`, MinGW-w64 GCC 16.2.0, Wine 8.16 `winebuild`, LLVM 15.0.7, and Apple Clang 21.0.0 for host tools. The build used Release mode, `enable_d3d12=true`, `enable_nvapi=false`, and `enable_nvngx=false`.

```sh
meson setup /tmp/dxmt-step10b-cross-d3d12 \
  /tmp/dxmt-fb451568-evidence.opkf8x/downstream \
  --cross-file /tmp/dxmt-fb451568-evidence.opkf8x/downstream/build-win64.txt \
  -Dnative_llvm_path=/tmp/dxmt-fb451568-evidence.opkf8x/clang+llvm-15.0.7-x86_64-apple-darwin21.0 \
  -Dwine_install_path=/tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain \
  -Denable_d3d12=true -Denable_nvapi=false -Denable_nvngx=false \
  --buildtype release
ninja -C /tmp/dxmt-step10b-cross-d3d12 -j4
```

**Result: PASS, 182/182 build targets.** The D3D12, D3D11, D3D10, DXGI, WineMetal PE DLLs, native WineMetal Unix library, and `airconv` host tool linked. Compilation emitted existing missing-return/uninitialized-value and macOS deprecation warnings, plus LLVM linker warnings; no target failed. DXMT source remained unmodified. This is a build result only; no Windows runtime or frame-generation behavior was exercised by this build.

The build tree is `/tmp/dxmt-step10b-cross-d3d12` (about 142 MiB at the final check). The retained SHA-256 identities are:

| Artifact | SHA-256 |
| --- | --- |
| `src/d3d12/d3d12.dll` | `a16c54fba7155dd26a2850a0fcd7568a55404e9e04a070442decbe1f91c14f05` |
| `src/d3d11/d3d11.dll` | `b16f528828c76bb79f8a35c61a2012d1a7c5a8d283abfece90aae2b68d5b962e` |
| `src/d3d10/d3d10core.dll` | `a551739148a8c82ed6b8ab0836fd810cdb4c1fb4f39c828c8a7a1b0bd928c1f6` |
| `src/dxgi/dxgi.dll` | `310be1aa3ef37ec839edf3d1253b8eaf2237f59c68ffc9b49e8b8c70106e4700` |
| `src/winemetal/winemetal.dll` | `c73c630a7d4be84012b17501660eaffb2d00cb55df18308b7aa66506fb1b3bcb` |
| `src/winemetal/unix/winemetal.so` | `539cb7a013ee8b9b5cc993e54841c9468a7beda6887b3d072188b7e5cb4a6973` |
| `src/airconv/darwin/airconv` | `2eb699138e4f478a0494ab7e14eb168ffbba61071ccd85833be6ca10ef583ede` |

At the final check, 40 GiB remained available on the host, above the 20 GiB stop threshold.
