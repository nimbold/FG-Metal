"""Serialized remaining public-state observations; stop on primary candidate.

No automatic Phase B or classification. Attainment is reviewed separately.
"""
import hashlib, json, subprocess, sys, time
from pathlib import Path
E = Path(__file__).resolve().parent
R = E.parents[1]
auth = sys.argv[1]
indices = [int(i) for i in sys.argv[2:]]
if not indices or any(i not in (3, 4, 5, 6, 8) for i in indices) or len(set(indices)) != len(indices):
    raise SystemExit('invalid series')
names = {3:'nonkey-frontmost',4:'partial-frontmost',5:'full-frontmost',6:'minimize-frontmost',8:'move-frontmost'}
records = []
for index in indices:
    tag = names[index]
    if any((E / f'{tag}-{suffix}').exists() for suffix in ('controller.json', 'sequence.log', 'analysis.json')):
        raise RuntimeError('refusing to overwrite a prior condition')
    with (E / f'{tag}-sequence.log').open('w') as log:
        rc = subprocess.run([sys.executable, str(E / 'window_sequence.py'), str(index), auth, tag, 'frontmost'], stdout=log, stderr=subprocess.STDOUT).returncode
    controller_path = E / f'{tag}-controller.json'
    controller = json.loads(controller_path.read_text()) if controller_path.exists() else []
    run_id = next((r.get('run_id') for r in controller if r.get('run_id')), None)
    record = dict(condition=index, tag=tag, controller_returncode=rc, run_id=run_id, finished_monotonic_ns=time.monotonic_ns())
    if rc or not run_id:
        record['series_stop_reason'] = 'controller or admission failed'
    else:
        output = E / f'{tag}-analysis.json'
        with (E / f'{tag}-analysis.log').open('w') as log:
            result = subprocess.run([sys.executable, str(R / 'br8_analysis.py'), str(R / 'runs' / run_id), '--output', str(output), '--controller-action-log', str(controller_path)], stdout=log, stderr=subprocess.STDOUT)
        record['analysis_returncode'] = result.returncode
        if result.returncode:
            record['series_stop_reason'] = 'strict analysis failed'
        else:
            analysis = json.loads(output.read_text())
            candidate = any(r['pathology_assessment']['sustained_pathology_observed'] for r in analysis['runs'])
            record['sustained_primary_candidate'] = candidate
            record['analysis_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
            if candidate:
                record['series_stop_reason'] = 'primary candidate; independent repeats and 300s confirmation required before further conditions'
    records.append(record)
    (E / 'phase-a-series-result.json').write_text(json.dumps(records, indent=2) + '\n')
    print(json.dumps(record), flush=True)
    if record.get('series_stop_reason'):
        break
