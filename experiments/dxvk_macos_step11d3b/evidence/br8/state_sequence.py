"""Serialized external focus control, frozen target tracing and authenticated runner.

Public AppKit activation is explicitly permitted by the BR8 request. Only the
inactive-app sequence has automatic actions; other conditions use normal UI.
"""
import subprocess,sys,time,json
from pathlib import Path
E=Path(__file__).resolve().parent;R=E.parents[1]
index=int(sys.argv[1]);smoke=sys.argv[2]
tag=sys.argv[3] if len(sys.argv)>3 else f'state-{index:02d}'
ui_driver=sys.argv[4] if len(sys.argv)>4 else 'api'
if ui_driver not in ('api','frontmost') or (ui_driver=='frontmost' and index!=2):raise SystemExit('invalid UI driver')
if not tag.replace('-','').isalnum():raise SystemExit('invalid evidence tag')
if index not in (1,2):raise SystemExit('automatic modes are only foreground 1 or inactive 2')
existing=set((R/'runs').glob('*'))
log=(E/f'{tag}-runner.log').open('w')
p=subprocess.Popen([sys.executable,str(R/'runner_b.py'),'state',str(index),smoke],stdout=log,stderr=subprocess.STDOUT)
records=[]; run=None;origin=None;pid=None
while p.poll() is None and origin is None:
 new=[d for d in (R/'runs').glob(f'step11d3b-br8-state-{index:02d}-*') if d not in existing]
 if len(new)>1:raise RuntimeError('ambiguous new run directory')
 if new:run=new[0]
 if run:
  for path in (run/'host-traces').glob('host-trace.*.jsonl'):
   with path.open() as f:
    for line in f:
     try:row=json.loads(line)
     except json.JSONDecodeError:continue
     if row.get('event')=='vkAcquireNextImageKHR_begin' and row.get('origin')=='MVK_BOUNDARY' and row.get('trace_role')=='target' and row.get('host_pid')==row.get('target_unix_pid'):
      origin=row['ts_mach_ns'];pid=row['host_pid'];break
   if origin:break
 time.sleep(.1)
if origin:
 records.append({'run_id':run.name,'origin_mach_ns':origin,'target_pid':pid,'condition_index':index})
 (E/f'{tag}-controller.json').write_text(json.dumps(records,indent=2)+'\n')
 print(json.dumps(records[-1]),flush=True)
 if index==2:
  schedule=[(1,pid),(10,None),(20,867),(25,None),(60,pid),(65,None)] if ui_driver=='frontmost' else [(10,None),(20,867),(25,None),(60,pid),(61,pid),(65,None)]
  for offset,actor in schedule:
   while p.poll() is None and time.monotonic_ns()<origin+offset*10**9:time.sleep(.05)
   if p.poll() is not None:records.append({'error':'runner ended before state action','offset':offset});break
   started=time.monotonic_ns()
   if actor is None:
    args=[sys.executable,str(E/'coverage_snapshot.py'),str(pid),str(E/f'{tag}-coverage-{offset}.json')]
   elif ui_driver=='frontmost':
    args=[sys.executable,str(E/'ax_window.py'),str(actor),'frontmost']
    if actor==867:args.append('Recents')
   elif offset==61:
    args=[sys.executable,str(E/'ax_window.py'),str(pid),'raise']
   else:
    args=[sys.executable,str(E/'app_activate.py'),str(actor)]
    if offset==20: args.append('com.apple.finder')
   a=subprocess.run(args,capture_output=True,text=True)
   records.append({'requested_loop_second':offset,'start_mach_ns':started,'end_mach_ns':time.monotonic_ns(),'actor_pid':actor,'returncode':a.returncode,'stdout':a.stdout,'stderr':a.stderr})
   (E/f'{tag}-controller.json').write_text(json.dumps(records,indent=2)+'\n')
   print(json.dumps(records[-1]),flush=True)
rc=p.wait();log.close()
records.append({'run_id':run.name if run else None,'runner_returncode':rc,'finished_mach_ns':time.monotonic_ns()})
(E/f'{tag}-controller.json').write_text(json.dumps(records,indent=2)+'\n')
print(json.dumps(records[-1]),flush=True)
raise SystemExit(rc)
