# DXMT Meson reconfigure rerun — aborted

Date: 2026-10-03 (Asia/Tehran)

The original clean native arm64 and Wine x86_64 builds remain the 10A build
evidence. A later optional `meson setup --reconfigure` / Ninja rebuild was
started to check the reproducibility instructions against the completed
cross-build tree. It caused Wine 8.16's `winebuild --builtin` post-processes to
stop producing output and enter macOS `U` (uninterruptible wait) state. The
Meson/Ninja parent was terminated; SIGTERM and SIGKILL did not clear the four
WineBuild processes. At the last check, these task-owned processes remained:

```text
33407 /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain/bin/winebuild --builtin src/winemetal/winemetal.dll
33410 /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain/bin/winebuild --builtin src/dxgi/dxgi.dll
33411 /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain/bin/winebuild --builtin src/d3d11/d3d11.dll
33412 /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain/bin/winebuild --builtin src/d3d10/d3d10core.dll
```

The reconfigure/rebuild retest is **ABORTED**, not a pass. It is not used as
evidence against the initial `[161/161]` clean cross-build, whose full Meson
setup and compile logs and whose runtime outputs are retained separately. Do
not relaunch this reconfigure against the same build tree until the blocked
WineBuild process state is understood. No source was modified by the retry.
