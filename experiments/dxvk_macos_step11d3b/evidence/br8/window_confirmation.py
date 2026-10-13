"""300s passive full-coverage confirmation, with public external AX controls.

Same entry at 20s as the short sequence; restoration at 280s permits a
260s hold and 20s restored tail. API success is not state attainment.
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

E = Path(__file__).resolve().parent
R = E.parents[1]
smoke, tag = sys.argv[1:]
if not tag.replace('-', '').isalnum():
    raise SystemExit('invalid tag')
if any((E / f'{tag}-{s}').exists() for s in ('controller.json', 'runner.log')):
    raise SystemExit('refusing evidence overwrite')
existing = set((R / 'runs').glob('*'))
records = [dict(controller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                helper_sha256={n: hashlib.sha256((E / n).read_bytes()).hexdigest()
                               for n in ('ax_window.py', 'coverage_snapshot.py')},
                protocol='20s pre / 260s full coverage / 20s restored',
                clock=time.get_clock_info('monotonic').implementation)]
log = (E / f'{tag}-runner.log').open('w')
p = subprocess.Popen([sys.executable, str(R / 'runner_b.py'), 'confirm', '1', smoke],
                     stdout=log, stderr=subprocess.STDOUT)
run = origin = pid = None

def save():
    (E / f'{tag}-controller.json').write_text(json.dumps(records, indent=2) + '\n')

try:
    while p.poll() is None and origin is None:
        new = [d for d in (R / 'runs').glob('step11d3b-br7-confirm-01-*') if d not in existing]
        if len(new) > 1:
            raise RuntimeError('ambiguous confirmation run')
        if new:
            run = new[0]
            for path in (run / 'host-traces').glob('host-trace.*.jsonl'):
                for line in path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (row.get('event') == 'vkAcquireNextImageKHR_begin'
                            and row.get('origin') == 'MVK_BOUNDARY'
                            and row.get('trace_role') == 'target'
                            and row.get('host_pid') == row.get('target_unix_pid')):
                        origin, pid = row['ts_mach_ns'], row['host_pid']
                        break
                if origin is not None:
                    break
        time.sleep(.1)
    if origin is None:
        raise RuntimeError('no target acquisition origin attained')
    records.append(dict(run_id=run.name, origin_mach_ns=origin, target_pid=pid,
                        condition_index=5, duration_seconds=300))
    save()

    def execute(offset, helper, args, label):
        while p.poll() is None and time.monotonic_ns() < origin + offset * 10**9:
            time.sleep(.05)
        if p.poll() is not None:
            raise RuntimeError('runner ended before scheduled action')
        start = time.monotonic_ns()
        result = subprocess.run([sys.executable, str(E / helper), *map(str, args)],
                                capture_output=True, text=True)
        record = dict(label=label, requested_loop_second=offset, start_mach_ns=start,
                      end_mach_ns=time.monotonic_ns(), returncode=result.returncode,
                      stdout=result.stdout, stderr=result.stderr)
        records.append(record)
        save()
        print(json.dumps(record), flush=True)
        if result.returncode:
            raise RuntimeError('external operation failed; no further mutations')

    execute(3, 'ax_window.py', [867, 'frontmost', 'Recents'], 'ordinary application foreground selection')
    execute(5, 'ax_window.py', [867, 'raise', 'Recents'], 'ordinary external window raise')
    execute(6, 'ax_window.py', [867, 'position', 'Recents', 734, 33], 'uncovered baseline')
    execute(10, 'coverage_snapshot.py', [pid, E / f'{tag}-pre-coverage.json'], 'baseline geometry')
    execute(20, 'ax_window.py', [867, 'position', 'Recents', 0, 33], 'coverage entry')
    for offset in (25, 100, 200, 275):
        execute(offset, 'coverage_snapshot.py', [pid, E / f'{tag}-hold-{offset}-coverage.json'], 'held geometry')
    execute(280, 'ax_window.py', [867, 'position', 'Recents', 734, 33], 'coverage restoration')
    execute(285, 'coverage_snapshot.py', [pid, E / f'{tag}-restored-coverage.json'], 'restored geometry')
except Exception as error:
    records.append(dict(controller_error=str(error), ts_mach_ns=time.monotonic_ns()))
    save()
rc = p.wait()
log.close()
records.append(dict(run_id=run.name if run else None, runner_returncode=rc,
                    finished_mach_ns=time.monotonic_ns()))
save()
print(json.dumps(records[-1]), flush=True)
raise SystemExit(rc if not any('controller_error' in r for r in records) else 1)
