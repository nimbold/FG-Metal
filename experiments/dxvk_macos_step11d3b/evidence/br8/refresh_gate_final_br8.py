import sys,subprocess,json
from pathlib import Path
E=Path(__file__).resolve().parent;repo=E.parents[3];storage=repo/'experiments/storage'
for stage,args in [('preflight',[str(storage/'storage_gate0.py'),'preflight']),('smoke',[str(storage/'smoke_prefix_lease.py')])]:
 p=E/f'gate0-final-br8-{stage}.log'
 with p.open('w') as f:r=subprocess.run([sys.executable,*args],stdout=f,stderr=subprocess.STDOUT)
 print(stage,r.returncode,flush=True)
 if r.returncode:raise SystemExit(r.returncode)
text=(E/'gate0-final-br8-smoke.log').read_text();record=json.loads(text[text.index('{\n'):]);report=Path(record['cleanup_receipt']).parent/'prefix-smoke-report.json'
(E/'gate0-final-br8-smoke-path.txt').write_text(str(report)+'\n')
with (E/'gate0-final-br8-finalize.log').open('w') as f:r=subprocess.run([sys.executable,str(storage/'storage_gate0.py'),'finalize','--smoke-report',str(report)],stdout=f,stderr=subprocess.STDOUT)
print('finalize',r.returncode,flush=True);raise SystemExit(r.returncode)
