#!/usr/bin/env python3
"""LOCAL DOWNSTREAM EXPERIMENT ONLY: bounded, fresh-engine matched capture."""
import csv, hashlib, json, os, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
SEED = Path('/private/tmp/dxmt-step10b5-run-v3/engine')
BUILD = Path('/private/tmp/fg-metal-step10b6_1-build')
LLVM = Path('/private/tmp/dxmt-fb451568-evidence.opkf8x/clang+llvm-15.0.7-x86_64-apple-darwin21.0/lib')
def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)
def rows(path):
    if not path.exists(): return []
    try:
        with path.open() as f: return list(csv.DictReader(f))
    except (OSError, csv.Error): return []
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    mode, name = sys.argv[1:3]
    seconds = int(sys.argv[3]) if len(sys.argv)>3 else 50
    generation = sys.argv[4] if len(sys.argv)>4 else '1'
    fail = sys.argv[5] if len(sys.argv)>5 else 'none'
    trace_seconds = int(sys.argv[6]) if len(sys.argv)>6 else 20
    if mode not in ('windowed','fullscreen') or not name.replace('-','').isalnum():
        raise SystemExit('invalid mode/name')
    temp = Path('/private/tmp') / ('step10b6_1-' + name)
    out = EVIDENCE / name
    if temp.exists() or out.exists(): raise SystemExit('refusing reused run paths')
    if shutil.disk_usage(ROOT).free < 22*1024**3: raise SystemExit('disk below 22GiB')
    temp.mkdir()
    run(ROOT/'tools/dxmt/copy_runtime_for_smoke.sh', SEED, temp/'engine')
    run(ROOT/'tools/dxmt/stage_dxmt_runtime.sh',temp/'engine',BUILD,LLVM)
    # Only the two changed runtime outputs are used; other DLLs stay frozen.
    for dll in ('d3d10core.dll','dxgi.dll','winemetal.dll'):
        shutil.copy2(SEED/'lib/wine/x86_64-windows'/dll,temp/'engine/lib/wine/x86_64-windows'/dll)
    env=os.environ.copy()
    env.update(DXMT_FRAMEGEN_START_FULLSCREEN='1' if mode=='fullscreen' else '0',
        DXMT_FRAMEGEN_EXPERIMENT_ALLOW_FULLSCREEN='1',
        DXMT_FRAMEGEN_EXPERIMENT_CONTROLLED_HARNESS='step10b6_1',
        DXMT_FRAMEGEN_LAYER_LOG=str(out/'layer-runtime.jsonl'),
        DXMT_FRAMEGEN_SIGNPOSTS='1',DXMT_FRAMEGEN_PREFERRED_FRAME_LATENCY='1',
        WINEDEBUG='-all')
    if os.environ.get('STEP10B6_1_GATE_ABSENT')=='1':
        env.pop('DXMT_FRAMEGEN_EXPERIMENT_ALLOW_FULLSCREEN',None)
    console = temp/'runner-console.log'
    with console.open('w') as f:
        process=subprocess.Popen([str(ROOT/'tools/dxmt/run_d3d11_framegen_experiment.sh'),
            str(temp/'engine'),str(temp/'prefix'),str(EVIDENCE/'matched-motion-probe.exe'),
            str(out),str(seconds),generation,'0',fail],env=env,stdout=f,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+100
    qualified=False
    ready=[]
    while time.monotonic()<deadline and process.poll() is None:
        ready=rows(out/'d3d11_clear_window_fullscreen.csv')
        native=rows(out/'native.csv')
        marker=[r for r in ready if r.get('event')=='measurement_ready']
        active=[r for r in native if r.get('event')=='session_active']
        primes=[r for r in native if r.get('event')=='experiment_two_fresh_sources']
        claims=[r for r in native if r.get('event')=='layer_owner_claimed']
        if marker and active and primes and claims:
            r=marker[-1]
            qualified=(r['fullscreen_state']==('1' if mode=='fullscreen' else '0') and
                r['visible']=='1' and r['iconic']=='0' and r['foreground']=='1' and r['hr']=='0x00000000')
            current_epoch=active[-1]['epoch']
            qualified=qualified and claims[-1]['epoch']==current_epoch and primes[-1]['epoch']==current_epoch
            qualified=qualified and native.index(primes[-1]) < native.index(claims[-1]) < native.index(active[-1])
            completed=[x for x in native[:native.index(primes[-1])] if x.get('event')=='source_escrow_complete' and x.get('epoch')==current_epoch]
            qualified=qualified and {primes[-1]['pair_a_id'],primes[-1]['pair_b_id']} <= {x['source_id'] for x in completed}
            if qualified: break
        time.sleep(.2)
    identity={'mode':mode,'name':name,'start_utc':datetime.now(timezone.utc).isoformat(),
        'seconds':seconds,'generation':generation,'failure':fail,'trace_seconds':trace_seconds,
        'pretrace_qualified':qualified,'cold_entry':True,'source_geometry':'640x360',
        'trace_profile':'Metal System Trace + Display + Points of Interest; attached Wine process',
        'environment':{k:v for k,v in env.items() if k.startswith('DXMT_FRAMEGEN')},
        'runtime_sha256':{},'app_sha256':digest(EVIDENCE/'matched-motion-probe.exe')}
    for rel in ['bin/wine','bin/wineserver','lib/wine/x86_64-windows/d3d10core.dll',
        'lib/wine/x86_64-windows/d3d11.dll','lib/wine/x86_64-windows/dxgi.dll',
        'lib/wine/x86_64-windows/winemetal.dll','lib/wine/x86_64-unix/winemetal.so',
        'lib/wine/x86_64-unix/libc++.1.dylib','lib/wine/x86_64-unix/libc++abi.1.dylib',
        'lib/wine/x86_64-unix/libunwind.1.dylib','lib/libinotify.0.dylib']:
        identity['runtime_sha256'][rel]=digest(temp/'engine'/rel)
    trace=temp/(name+'.trace')
    if qualified and trace_seconds:
        # Wine changes its argv to the Windows executable title after startup.
        # The runner's only direct child is the launched Wine app; use lineage.
        ps=subprocess.check_output(['ps','-axo','pid=,ppid=,comm=,args='],text=True)
        matches=[line.split(None,3) for line in ps.splitlines()]
        matches=[m for m in matches if len(m)>3 and m[1]==str(process.pid)]
        if len(matches)!=1:
            raise RuntimeError('ambiguous Wine process: '+repr(matches))
        pid=matches[0][0]
        identity['trace_process_lineage']=matches[0]
        identity['trace_pid']=pid
        with (out/'xctrace-console.log').open('w') as f:
            run('xcrun','xctrace','record','--template','Metal System Trace','--instrument','Display',
                '--instrument','Points of Interest','--attach',pid,'--time-limit',str(trace_seconds)+'s',
                '--output',trace,'--no-prompt',stdout=f,stderr=subprocess.STDOUT)
    status=process.wait(timeout=seconds+30)
    shutil.copy2(console,out/'runner-console.log')
    identity['runner_exit']=status
    (out/'run-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
    if qualified and trace_seconds:
        run('python3',EVIDENCE.parent/'step10b6/export_display_trace.py',trace,out)
        with (out/'display-analysis-console.json').open('w') as f:
            run('python3',EVIDENCE.parent/'step10b6/analyze_display_trace.py',out,'--process','wine',
                '--dxmt-native-csv',out/'native.csv','--json-output',out/'display-analysis.json',stdout=f)
        run('python3',EVIDENCE/'qualify_trace.py',out)
    print(json.dumps({'name':name,'qualified':qualified,'runner_exit':status,'trace':str(trace)},indent=2))
    return 0 if qualified and status==0 else 1
if __name__=='__main__': raise SystemExit(main())
