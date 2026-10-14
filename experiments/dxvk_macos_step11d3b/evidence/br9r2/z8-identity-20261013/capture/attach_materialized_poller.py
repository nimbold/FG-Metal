#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,pathlib,subprocess,time
from datetime import datetime,timezone
ROOT=pathlib.Path('/private/tmp/codex-br9r2-z8-identity-capture')
ACTIVE=pathlib.Path('/Users/nima/Library/Caches/FGMetal/prefixes/.active-leases')
POLLER=ROOT/'public_cgwindow_inventory'
OUT=ROOT/'public-cgwindow-inventory.jsonl'
LAUNCH=ROOT/'poller-launch.json'
ATTACH=ROOT/'poller-attachment-result.json'

def utc():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):
 t=p.with_name('.'+p.name+'.tmp'); raw=(json.dumps(v,indent=2,sort_keys=True)+'\n').encode()
 fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 os.replace(t,p)

def markers():
 found=[]
 for p in ACTIVE.glob('step11d3b-smoke-*.json'):
  if p.is_symlink() or not p.is_file():raise RuntimeError('unsafe active marker')
  try:v=json.loads(p.read_text())
  except Exception:continue
  found.append((p,v))
 return found

start=time.monotonic_ns()
if OUT.exists() or LAUNCH.exists() or ATTACH.exists():raise RuntimeError('attachment output already exists')
chosen=None
while time.monotonic()-start/1e9 if False else True:
 rows=markers()
 candidates=[(p,v) for p,v in rows if v.get('state') in {'PREPARED','MATERIALIZED'}]
 if len(candidates)>1:raise RuntimeError('multiple active STEP 11D.3-B smoke markers')
 if candidates:
  chosen=candidates[0]
  if chosen[1].get('state')=='MATERIALIZED':break
 elif not rows:
  if (ROOT/'experiment-complete.json').exists():break
  time.sleep(.1)
  # Continue through a short marker write/read gap; parent controller is authoritative.
  if time.monotonic_ns()-start > 1_300_000_000_000:break
  continue
 else:
  time.sleep(.1);continue
 time.sleep(.1)

if chosen is None or chosen[1].get('state')!='MATERIALIZED':
 write(ATTACH,{'status':'NO_MATERIALIZED_MARKER','finished_at_utc':utc(),'markers':[{'path':str(p),'state':v.get('state'),'experiment_id':v.get('experiment_id')} for p,v in markers()]})
 raise SystemExit(1)
marker_path,marker=chosen
records=sorted(ROOT.glob('*-controller.json'),key=lambda p:p.stat().st_mtime_ns)
controller_record=next((json.loads(p.read_text()) for p in reversed(records) if p.is_file()),None)
if not isinstance(controller_record,dict) or not isinstance(controller_record.get('observer'),dict):raise RuntimeError('lifecycle observer controller record missing')
observer=controller_record['observer'];handoff=observer.get('handoff_path');completion=observer.get('completion_path')
if not isinstance(handoff,str) or not isinstance(completion,str):raise RuntimeError('lifecycle handoff/completion path missing')
meta={'status':'RUNNING','started_at_utc':utc(),'started_monotonic_ns':time.monotonic_ns(),'marker_path':str(marker_path),'marker_sha256':sha(marker_path),'experiment_id':marker.get('experiment_id'),'prefix_lease_pid':marker.get('pid'),'state':marker.get('state'),'handoff_path':handoff,'completion_path':completion,'output_path':str(OUT),'poller_source_sha256':sha(ROOT/'public_cgwindow_inventory.swift'),'poller_binary_sha256':sha(POLLER),'poll_interval_ms':250}
write(LAUNCH,meta)
with (ROOT/'public-cgwindow-inventory.stderr.log').open('xb') as log:
 proc=subprocess.Popen([str(POLLER),handoff,completion,str(OUT),'250'],stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
 meta['poller_pid']=proc.pid;meta['poller_started_after_materialized_marker']=True;write(LAUNCH,meta)
 rc=proc.wait()
write(ATTACH,{'status':'FINISHED','finished_at_utc':utc(),'poller_returncode':rc,'poller_output_path':str(OUT) if OUT.exists() else None,'poller_output_sha256':sha(OUT) if OUT.exists() else None,'poller_launch':meta})
raise SystemExit(rc)
