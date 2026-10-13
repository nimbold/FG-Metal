import sys,time,json
from pathlib import Path
E=Path(__file__).resolve().parent
row={'mach_monotonic_ns':time.monotonic_ns(),'run_id':sys.argv[1],'action':sys.argv[2],'edge':sys.argv[3],'note':' '.join(sys.argv[4:])}
with (E/'controller-actions.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
print(json.dumps(row))
