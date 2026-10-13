import json,sys,time
from pathlib import Path
R=Path(__file__).resolve().parents[2]
run=Path(sys.argv[1]) if len(sys.argv)>1 else max((R/'runs').glob('*'),key=lambda p:p.stat().st_mtime_ns)
out={'run':str(run),'now_mach_ns':time.monotonic_ns()}
for path in (run/'host-traces').glob('host-trace.*.jsonl'):
 first=None; latest=None; notifications=[]
 with path.open() as f:
  for line in f:
   try: row=json.loads(line)
   except json.JSONDecodeError: continue
   if row.get('trace_role')!='target': continue
   if first is None and row.get('event')=='vkAcquireNextImageKHR_begin': first=row.get('ts_mach_ns')
   if row.get('event')=='window_telemetry':
    latest=row
    if row.get('notification_name'): notifications.append(row)
 if first:
  out.update(origin_mach_ns=first,elapsed_s=(time.monotonic_ns()-first)/1e9,target_pid=latest.get('host_pid') if latest else None,latest=latest,notifications=notifications)
print(json.dumps(out,indent=2))
