"""External normal AX window actions; frozen authenticated target runner.

Coverage trials hold Finder active throughout and move only its Recents window.
Minimize/restore and movement operate on the exact PID/title-bound target.
Actual state attainment is determined offline from telemetry, not API success.
"""
import json, subprocess, sys, time
from pathlib import Path

E = Path(__file__).resolve().parent
R = E.parents[1]
index, smoke, tag = int(sys.argv[1]), sys.argv[2], sys.argv[3]
ui_driver = sys.argv[4] if len(sys.argv) > 4 else 'api'
if ui_driver not in ('api', 'cua', 'frontmost') or (ui_driver == 'cua' and index not in (4, 5)):
    raise SystemExit('invalid UI driver')
if index not in (3, 4, 5, 6, 8) or not tag.replace('-', '').isalnum():
    raise SystemExit('invalid external sequence')
existing = set((R / 'runs').glob('*'))
records = []
log = (E / f'{tag}-runner.log').open('w')
p = subprocess.Popen([sys.executable, str(R / 'runner_b.py'), 'state', str(index), smoke], stdout=log, stderr=subprocess.STDOUT)
run = origin = pid = None
while p.poll() is None and origin is None:
    new = [d for d in (R / 'runs').glob(f'step11d3b-br8-state-{index:02d}-*') if d not in existing]
    if len(new) > 1:
        raise RuntimeError('ambiguous run')
    if new:
        run = new[0]
        for path in (run / 'host-traces').glob('host-trace.*.jsonl'):
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get('event') == 'vkAcquireNextImageKHR_begin' and row.get('origin') == 'MVK_BOUNDARY' and row.get('trace_role') == 'target' and row.get('host_pid') == row.get('target_unix_pid'):
                    origin, pid = row['ts_mach_ns'], row['host_pid']
                    break
            if origin:
                break
    time.sleep(.1)

def save():
    (E / f'{tag}-controller.json').write_text(json.dumps(records, indent=2) + '\n')

def execute(offset, helper, args, label):
    while p.poll() is None and time.monotonic_ns() < origin + offset * 10**9:
        time.sleep(.05)
    if p.poll() is not None:
        raise RuntimeError('runner ended before scheduled external action')
    start = time.monotonic_ns()
    result = subprocess.run([sys.executable, str(E / helper), *map(str, args)], capture_output=True, text=True)
    record = dict(label=label, requested_loop_second=offset, start_mach_ns=start, end_mach_ns=time.monotonic_ns(), returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    records.append(record)
    save()
    print(json.dumps(record), flush=True)
    if result.returncode:
        raise RuntimeError('external operation failed; no further mutations')

if origin:
    records.append(dict(run_id=run.name, origin_mach_ns=origin, target_pid=pid, condition_index=index))
    save()
    print(json.dumps(records[-1]), flush=True)
    try:
        if index == 3:
            if ui_driver == 'frontmost':
                execute(1, 'ax_window.py', [pid, 'frontmost'], 'foreground baseline selection')
                execute(20, 'ax_window.py', [867, 'frontmost', 'Recents'], 'other application focus')
            else:
                execute(20, 'app_activate.py', [867, 'com.apple.finder'], 'other application focus')
                execute(21, 'finder_new_window.py', [], 'ordinary second application window')
            execute(22, 'ax_window.py', [867, 'position', 'Recents', 734, 33], 'uncovered external window')
            execute(23, 'ax_window.py', [867, 'raise', 'Recents'], 'other application window raise')
            execute(25, 'coverage_snapshot.py', [pid, E / f'{tag}-hold-coverage.json'], 'held nonkey geometry')
            if ui_driver == 'frontmost':
                execute(60, 'ax_window.py', [pid, 'frontmost'], 'foreground restore request')
            else:
                execute(60, 'app_activate.py', [pid], 'foreground restore request')
            execute(61, 'ax_window.py', [pid, 'raise'], 'ordinary target raise')
        elif index in (4, 5):
            if ui_driver == 'api':
                execute(3, 'app_activate.py', [867, 'com.apple.finder'], 'matched inactive baseline')
                execute(4, 'finder_new_window.py', [], 'ensure ordinary external covering window')
                execute(5, 'ax_window.py', [867, 'raise', 'Recents'], 'raise external covering window')
            elif ui_driver == 'frontmost':
                execute(3, 'ax_window.py', [867, 'frontmost', 'Recents'], 'ordinary application foreground selection')
                execute(5, 'ax_window.py', [867, 'raise', 'Recents'], 'ordinary external window raise')
            execute(6, 'ax_window.py', [867, 'position', 'Recents', 734, 33], 'uncovered baseline')
            execute(10, 'coverage_snapshot.py', [pid, E / f'{tag}-pre-coverage.json'], 'baseline geometry')
            execute(20, 'ax_window.py', [867, 'position', 'Recents', 367 if index == 4 else 0, 33], 'coverage entry')
            execute(25, 'coverage_snapshot.py', [pid, E / f'{tag}-hold-coverage.json'], 'held geometry')
            execute(60, 'ax_window.py', [867, 'position', 'Recents', 734, 33], 'coverage restoration')
        elif index == 6:
            if ui_driver == 'frontmost':
                execute(1, 'ax_window.py', [pid, 'frontmost'], 'foreground baseline selection')
            execute(20, 'ax_window.py', [pid, 'minimize'], 'normal miniaturization')
            execute(60, 'ax_window.py', [pid, 'restore'], 'normal deminiaturization')
            if ui_driver == 'frontmost':
                execute(61, 'ax_window.py', [pid, 'frontmost'], 'foreground restoration')
            else:
                execute(61, 'app_activate.py', [pid], 'foreground restoration')
        elif index == 8:
            if ui_driver == 'frontmost':
                execute(1, 'ax_window.py', [pid, 'frontmost'], 'foreground baseline selection')
            execute(20, 'ax_window.py', [pid, 'position', 'FG-Metal Step 11D.3-B Bare Vulkan', 400, 62], 'visible movement')
            execute(60, 'ax_window.py', [pid, 'position', 'FG-Metal Step 11D.3-B Bare Vulkan', 68, 62], 'position restoration')
        execute(65, 'coverage_snapshot.py', [pid, E / f'{tag}-restored-coverage.json'], 'restored geometry')
    except Exception as error:
        records.append(dict(controller_error=str(error), ts_mach_ns=time.monotonic_ns()))
        save()
rc = p.wait()
log.close()
records.append(dict(run_id=run.name if run else None, runner_returncode=rc, finished_mach_ns=time.monotonic_ns()))
save()
print(json.dumps(records[-1]), flush=True)
raise SystemExit(rc if not any('controller_error' in row for row in records) else 1)
