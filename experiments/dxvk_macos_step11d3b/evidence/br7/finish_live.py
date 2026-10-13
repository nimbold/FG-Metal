import json,subprocess,sys
from pathlib import Path
E=Path(__file__).resolve().parent
R=E.parents[1]
rows=json.loads((E/'short-series.json').read_text())
assert len(rows)==6 and all(x['is_valid'] for x in rows)
args=[sys.executable,str(R/'br7_analysis.py'),'--output-dir',str(E/'short-analysis')]
for x in rows: args += ['--run-dir',str(R/'runs'/x['run_id'])]
subprocess.run(args,check=True)
a=json.loads((E/'short-analysis/summary.json').read_text())['runs']
labels=[x['regime_analysis']['overall_label'] for x in a]
print('SIX CLASSIFICATIONS',labels,flush=True)
mode,index=('baseline','2') if all(x=='HEALTHY' for x in labels) else ('confirm','1')
with (E/'additional-reproduction.log').open('w') as log:
 p=subprocess.run([sys.executable,str(R/'runner_b.py'),mode,index,'step11d3b-smoke-20261010-074106-d99e7461'],stdout=log,stderr=subprocess.STDOUT)
pattern='step11d3b-baseline-02-*' if mode=='baseline' else 'step11d3b-br7-confirm-01-*'
d=sorted((R/'runs').glob(pattern),key=lambda p:p.stat().st_mtime)[-1]
r=json.loads((d/'run-result.json').read_text())
(E/'additional-run.json').write_text(json.dumps({'mode':mode,'index':index,'run_id':d.name,'returncode':p.returncode,'result':r},indent=2)+'\n')
print(d.name,r['status'],r.get('is_valid'),flush=True)
assert p.returncode==0 and r.get('is_valid') is True
