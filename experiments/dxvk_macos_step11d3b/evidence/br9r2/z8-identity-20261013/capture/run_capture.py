#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, os, pathlib, signal, subprocess, sys, time
from datetime import datetime, timezone

REPO = pathlib.Path('/Users/nima/Documents/Code/FG-Metal')
ROOT = pathlib.Path('/private/tmp/codex-br9r2-z8-identity-capture')
ACTIVE = pathlib.Path('/Users/nima/Library/Caches/FGMetal/prefixes/.active-leases')
CONTROLLER = REPO / 'experiments/dxvk_macos_step11d3b/evidence/br9r2/authorization_smoke_controller.py'
POLLER = ROOT / 'public_cgwindow_inventory'
PYTHON = pathlib.Path('/opt/homebrew/bin/python3.14')
RESULT = ROOT / 'capture-wrapper-result.json'
POLL_OUTPUT = ROOT / 'public-cgwindow-inventory.jsonl'


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    tmp=path.with_name('.'+path.name+'.tmp')
    raw=(json.dumps(value,indent=2,sort_keys=True)+'\n').encode()
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
        d=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try: os.fsync(d)
        finally: os.close(d)
    except BaseException:
        try: tmp.unlink()
        except FileNotFoundError: pass
        raise


def active_markers():
    out=[]
    if not ACTIVE.is_dir() or ACTIVE.is_symlink():
        raise RuntimeError('active lease root is unavailable or unsafe')
    for path in sorted(ACTIVE.glob('*.json')):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f'unsafe active lease marker: {path}')
        value=json.loads(path.read_text())
        out.append((path,value))
    return out


if ROOT.resolve(strict=True) != ROOT or ROOT.parent != pathlib.Path('/private/tmp'):
    raise RuntimeError('capture root is not canonical')
if os.stat(ROOT).st_uid != os.getuid() or (os.stat(ROOT).st_mode & 0o777) != 0o700:
    raise RuntimeError('capture root must be owned and mode 0700')
if not PYTHON.is_file() or not CONTROLLER.is_file() or not POLLER.is_file():
    raise RuntimeError('one-run controller or reviewed poller is unavailable')
if RESULT.exists() or POLL_OUTPUT.exists():
    raise RuntimeError('refusing to reuse a capture root with existing result/output')
initial=active_markers()
if initial:
    write_json(RESULT,{'status':'REFUSED_ACTIVE_LEASE','active_markers':[{'path':str(p),'experiment_id':v.get('experiment_id'),'pid':v.get('pid'),'state':v.get('state')} for p,v in initial], 'finished_at_utc':utc()})
    raise SystemExit(2)

env=os.environ.copy()
env['FGMETAL_BR9R_EVIDENCE_ROOT']=str(ROOT)
controller_log=ROOT/'authorization-smoke-controller.stdout.log'
controller=None
poller=None
controller_record_path=None
poller_launch=None
poller_returncode=None
controller_returncode=None
poller_terminated_by_wrapper=False
started_ns=time.monotonic_ns()
with controller_log.open('xb') as log:
    controller=subprocess.Popen([str(PYTHON),str(CONTROLLER)],cwd=str(REPO),env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
    while controller.poll() is None:
        records=sorted(ROOT.glob('*-controller.json'), key=lambda p:p.stat().st_mtime_ns)
        current=None
        for path in reversed(records):
            try:
                value=json.loads(path.read_text())
            except (OSError,json.JSONDecodeError):
                continue
            if isinstance(value,dict) and value.get('runner_pid'):
                current=(path,value)
                break
        if current is not None:
            controller_record_path,record=current
            runner_pid=record.get('runner_pid')
            if poller is None and isinstance(runner_pid,int):
                match=None
                for marker_path,marker in active_markers():
                    if marker.get('pid')==runner_pid and marker.get('state')=='MATERIALIZED':
                        match=(marker_path,marker)
                        break
                if match is not None:
                    marker_path,marker=match
                    observer=record.get('observer') if isinstance(record.get('observer'),dict) else {}
                    handoff=observer.get('handoff_path')
                    completion=observer.get('completion_path')
                    if not isinstance(handoff,str) or not isinstance(completion,str):
                        raise RuntimeError('controller record lacks lifecycle-bound handoff/completion paths')
                    poller_launch={
                        'status':'STARTING', 'started_at_utc':utc(),
                        'started_monotonic_ns':time.monotonic_ns(),
                        'marker_path':str(marker_path), 'marker_sha256':sha(marker_path),
                        'experiment_id':marker.get('experiment_id'), 'runner_pid':runner_pid,
                        'state':marker.get('state'), 'handoff_path':handoff,
                        'completion_path':completion, 'output_path':str(POLL_OUTPUT),
                        'poller_source_sha256':sha(ROOT/'public_cgwindow_inventory.swift'),
                        'poller_binary_sha256':sha(POLLER), 'poll_interval_ms':250,
                        'controller_record_path':str(controller_record_path),
                    }
                    poller_log=(ROOT/'public-cgwindow-inventory.stderr.log').open('xb')
                    poller=subprocess.Popen([str(POLLER),handoff,completion,str(POLL_OUTPUT),'250'],cwd=str(REPO),stdout=poller_log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
                    poller_launch['poller_pid']=poller.pid
                    poller_launch['status']='RUNNING'
                    write_json(ROOT/'poller-launch.json',poller_launch)
        time.sleep(0.1)
    controller_returncode=controller.wait()

if poller is not None:
    try:
        poller_returncode=poller.wait(timeout=45)
    except subprocess.TimeoutExpired:
        poller.terminate()
        poller_terminated_by_wrapper=True
        try: poller_returncode=poller.wait(timeout=5)
        except subprocess.TimeoutExpired:
            poller.kill()
            poller_returncode=poller.wait(timeout=10)

result={
    'status':'COMPLETED', 'started_at_utc':utc(), 'started_monotonic_ns':started_ns,
    'finished_at_utc':utc(), 'finished_monotonic_ns':time.monotonic_ns(),
    'controller_path':str(CONTROLLER), 'controller_sha256':sha(CONTROLLER),
    'controller_pid':controller.pid if controller else None,
    'controller_returncode':controller_returncode,
    'controller_record_path':str(controller_record_path) if controller_record_path else None,
    'controller_stdout_path':str(controller_log),
    'poller_source_sha256':sha(ROOT/'public_cgwindow_inventory.swift'),
    'poller_binary_sha256':sha(POLLER),
    'poller_launch':poller_launch,
    'poller_returncode':poller_returncode,
    'poller_output_path':str(POLL_OUTPUT) if POLL_OUTPUT.exists() else None,
    'poller_output_sha256':sha(POLL_OUTPUT) if POLL_OUTPUT.exists() else None,
    'poller_stderr_path':str(ROOT/'public-cgwindow-inventory.stderr.log') if (ROOT/'public-cgwindow-inventory.stderr.log').exists() else None,
    'poller_terminated_by_wrapper':poller_terminated_by_wrapper,
}
write_json(RESULT,result)
print(json.dumps(result,indent=2,sort_keys=True))
raise SystemExit(0 if controller_returncode==0 else 1)
