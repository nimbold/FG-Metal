# x86_64 runtime stalled before the first architecture log

The x86_64 target configured and linked successfully. `file` reported a
`Mach-O 64-bit executable x86_64`, and verbose compiler/linker commands included
`-arch x86_64`. The process stalled before its first architecture log line or
any display callback. The requested capture duration below was two seconds;
that argument did not impose a process-level wall-time timeout.

The smoke command, requesting a two-second capture, was:

```sh
arch -x86_64 \
  build/step10b-synthetic-x86_64/framegen-dxmt-synthetic-metal.app/Contents/MacOS/framegen-dxmt-synthetic-metal \
  --duration-seconds 2 \
  --output build/step10b-synthetic-x86_64/smoke-evidence \
  > build/step10b-synthetic-x86_64/smoke.log 2>&1
```

At 2026-10-03 02:50 +0330, the two retained x86_64 instances were:

| PID | State | Elapsed | CPU | Command |
|---|---|---:|---:|---|
| 33739 | `Us` | 21:47 | 0:00.06 | `...framegen-dxmt-synthetic-metal --duration-seconds 2 --output .../smoke-evidence` |
| 32015 | `Us` | 54:20 | 0:00.05 | `./...framegen-dxmt-synthetic-metal` |

The smoke log is intentionally retained as a zero-byte file. No architecture or
Metal-device line was printed and no display callbacks began. In current source,
`MTLCreateSystemDefaultDevice()` is called before the architecture line, so it
is a plausible location for the pre-log stall. No stack trace was captured to
attribute the stall to that function. `TERM` and `KILL` did not remove these
`Us` tasks at the time. At the final host check after the arm64 v4 capture,
neither PID was present in `ps`; their eventual exit time and cause were not
observed. No additional x86_64 relaunch was made after recording the startup
stall.

Native arm64 callbacks do not resolve the x86_64/Rosetta startup issue and are
kept separately as diagnostic evidence only.
