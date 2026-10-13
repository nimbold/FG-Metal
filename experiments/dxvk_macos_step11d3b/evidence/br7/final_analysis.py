import json,subprocess,sys
from pathlib import Path
E=Path(__file__).resolve().parent;R=E.parents[1]
rows=json.loads((E/'short-series.json').read_text());extra=json.loads((E/'additional-run.json').read_text());assert extra['result']['is_valid']
ids=['step11d3b-smoke-20261010-074106-d99e7461']+[r['run_id'] for r in rows]+[extra['run_id']]
args=[sys.executable,str(R/'br7_analysis.py'),'--output-dir',str(E/'new-analysis')]
for rid in ids:args+=['--run-dir',str(R/'runs'/rid)]
subprocess.run(args,check=True)
subprocess.run([sys.executable,str(E/'mode_statistics.py'),str(E/'new-analysis')],check=True)
subprocess.run([sys.executable,str(E/'state_comparison.py'),str(E/'new-analysis')],check=True)
subprocess.run([sys.executable,str(E/'compare_launch.py')],check=True)
sys.path.insert(0,str(R))
import runner_b
storage=runner_b.gate0_snapshot('BR7_FINAL')
(E/'final-storage.json').write_text(json.dumps(storage,indent=2)+'\n')
print(json.dumps(storage),flush=True)
subprocess.run([sys.executable,str(E/'write_report.py')],check=True)
subprocess.run([sys.executable,str(E/'enrich_report.py')],check=True)
