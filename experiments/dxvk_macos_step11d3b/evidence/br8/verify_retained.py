import sys,json,hashlib
from pathlib import Path
R=Path(__file__).resolve().parents[2];sys.path.insert(0,str(R))
import br7_analysis as a
ids=['step11d3b-smoke-20261010-053718-a78894d0','step11d3b-baseline-01-20261009-223854-cfea0560','step11d3b-baseline-02-20261010-054210-3a16e5aa','step11d3b-baseline-03-20261010-062321-91bb8f4d','step11d3b-smoke-20261010-074106-d99e7461']
ids += [r['run_id'] for r in json.loads((R/'evidence/br7/short-series.json').read_text())]
ids += ['step11d3b-baseline-02-20261010-084004-9ef35a0d']
rows=[]
for rid in ids:
 data=a.analyze_run(R/'runs'/rid)
 rows.append(data['summary'])
 print(rid,flush=True)
(R/'evidence/br8/retained-reverification.json').write_text(json.dumps(rows,indent=2)+'\n')
