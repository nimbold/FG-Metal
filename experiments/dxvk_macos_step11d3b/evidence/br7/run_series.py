"""Six serialized BR7 observation samples, each retaining full runner admission."""
import json,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
evidence=Path(__file__).resolve().parent
smoke=sys.argv[1]
rows=json.loads((evidence/"short-series.json").read_text()) if (evidence/"short-series.json").exists() else []
for index in range(len(rows)+1,7):
 print(f'START sample {index}',flush=True)
 with (evidence/f'sample-{index:02d}.log').open('w') as log:
  process=subprocess.run([sys.executable,str(root/'runner_b.py'),'sample',str(index),smoke],stdout=log,stderr=subprocess.STDOUT)
 candidates=sorted((root/'runs').glob(f'step11d3b-br7-sample-{index:02d}-*'),key=lambda p:p.stat().st_mtime)
 run=candidates[-1] if candidates else None
 result=json.loads((run/'run-result.json').read_text()) if run and (run/'run-result.json').exists() else {}
 validation=result.get('validation') or {}
 row={'index':index,'run_id':run.name if run else None,'returncode':process.returncode,'status':result.get('status'),'is_valid':result.get('is_valid'),'quantitative_ready':validation.get('quantitative_ready'),'acquired_fps':validation.get('acquired_fps'),'displayed_fps':validation.get('presented_fps'),'frame_count':validation.get('frame_count'),'displayed_frames':validation.get('displayed_frames'),'next_drawable_max_ns':validation.get('next_drawable',{}).get('maximum_ns'),'gate0_pre':result.get('gate0_pre'),'gate0_post':result.get('gate0_post'),'prefix_cleanup_pass':result.get('prefix_cleanup_pass')}
 rows.append(row)
 (evidence/'short-series.json').write_text(json.dumps(rows,indent=2)+'\n')
 print(json.dumps({k:v for k,v in row.items() if not k.startswith('gate0')}),flush=True)
 if process.returncode or result.get('is_valid') is not True:
  raise SystemExit('Stopped: sample was not accepted; inspect retained evidence.')
