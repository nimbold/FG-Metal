#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path

BASE=Path(__file__).resolve().parent
REPO=Path(__file__).resolve().parents[4]
BUILD=Path('/private/tmp/fgmetal-step11d-r-current-r5-build-A-20261006')
BRIDGE=Path('/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof/build/status-v3-current-20261006')
APP_SOURCE=Path('/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/renderer-integration/slow-visual-metal.exe')
RUNTIME=Path('/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine')
ICD=Path('/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json')
LOADER=BRIDGE/'mvk142-loader-override'
PINNED=Path('/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib')
EXPECTED={'d3d11.dll':'e747ca5a5c2087d1f788762a5c4bae13939593a2cb89dedf2f7c04999ab94de5','dxgi.dll':'58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132','FGMetalBridge.dll':'44cdaa3ff503a1beb3afd735620c3b33eac4bca17377fa78e5c3be5f852c18cf','fgmetalbridge.so':'033e3a8cbd957b1e16131fc690256aba8684d51b17688514a5ff7eaa351c2fe2'}
APP_SHA='f9e581029c215edc2ecad46f5c22cc1b0b0cfc8d17fbaec5f7d96667fc556169'

def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def active(log):
    s=log.read_text(errors='replace') if log.exists() else ''
    return (s.count('MetalInterop: captured InteropJobId='),s.count('MetalInterop: committed InteropJobId='),s.count('MetalInterop: job-retired InteropJobId='),s)
def main():
    actual={'d3d11.dll':sha(BUILD/'src/d3d11/d3d11.dll'),'dxgi.dll':sha(BUILD/'src/dxgi/dxgi.dll'),'FGMetalBridge.dll':sha(BRIDGE/'overlay/x86_64-windows/FGMetalBridge.dll'),'fgmetalbridge.so':sha(BRIDGE/'overlay/x86_64-unix/fgmetalbridge.so')}
    if actual!=EXPECTED or sha(APP_SOURCE)!=APP_SHA: raise SystemExit(f'frozen identity mismatch: {actual} app={sha(APP_SOURCE)}')
    all_results=[]
    for offset in (1,5,10):
        name=f'offset-{offset:02d}s'; out=BASE/name
        prefix=Path(f'/Users/nima/Library/Caches/FGMetalStep11D-R/step11d1-process-{name}-20261006-prefix')
        if out.exists() or prefix.exists(): raise SystemExit(f'refusing existing run: {out}, {prefix}')
        out.mkdir(); overlay=out/'overlay'; shutil.copytree(BRIDGE/'overlay',overlay)
        shutil.copy2(BUILD/'src/d3d11/d3d11.dll',out/'d3d11.dll'); shutil.copy2(BUILD/'src/dxgi/dxgi.dll',out/'dxgi.dll'); shutil.copy2(APP_SOURCE,out/'slow-visual.exe')
        (out/'dxvk.conf').write_text('dxvk.enableMetalInterop = True\n')
        env=os.environ.copy()
        for k in list(env):
            if k.startswith(('WINE','DXVK_','FG_','MVK_','MTL_','VK_','DYLD_')): env.pop(k,None)
        rp=[RUNTIME/'lib/wine/x86_64-unix',RUNTIME/'lib/wine/x86_64-windows',RUNTIME/'lib/wine/i386-windows']
        dyld=':'.join((str(LOADER),str(rp[0]),'/private/tmp/fgmetal-vulkan-link')); fallback=':'.join((dyld,str(RUNTIME/'lib'),'/usr/local/lib','/usr/lib'))
        env.update({'WINEPREFIX':str(prefix),'WINEARCH':'win64','WINELOADER':str(RUNTIME/'bin/wine'),'WINESERVER':str(RUNTIME/'bin/wineserver'),'WINEDATADIR':str(RUNTIME/'share'),'WINEDLLPATH':':'.join((str(overlay),*(str(p) for p in rp))),'WINEDLLOVERRIDES':'d3d11,dxgi=n,b','WINEDEBUG':'+err,+loaddll','VK_ICD_FILENAMES':str(ICD),'VK_DRIVER_FILES':str(ICD),'DYLD_LIBRARY_PATH':dyld,'DYLD_FALLBACK_LIBRARY_PATH':fallback,'DYLD_PRINT_LIBRARIES':'1','DXVK_CONFIG_FILE':str(out/'dxvk.conf'),'DXVK_LOG_LEVEL':'info','DXVK_LOG_PATH':str(out),'DXVK_INTERNAL_WSI_TRACE':'1','FG_AUDIT_ENABLE':'1','FG_AUDIT_RUN_SECONDS':'60','FG_AUDIT_LIFECYCLE_TEST':'0','FG_AUDIT_SKIP_RESIZE':'1','FG_AUDIT_WAITABLE':'1','FG_AUDIT_SOURCE_HZ':'30','FG_AUDIT_FRAMES_PER_COLOR':'30','FG_AB_BGRA':'0'})
        manifest={'offset_seconds_after_active_commit':offset,'source_hz':30,'test_run_duration_seconds':60,'dll_sha256':actual,'app_sha256':sha(out/'slow-visual.exe'),'source_build_id':'current-r5-build-A-20261006','native_provider_build_id':'status-v3-current-20261006','termination_method':'Wine taskkill /F /IM slow-visual.exe','screen_capture':False,'cpu_pixel_readback':False}
        (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        subprocess.run([sys.executable,str(REPO/'experiments/storage/prefix_preflight.py'),str(prefix),str(Path(__file__).resolve())],check=True)
        with (out/'wineboot.log').open('w') as f: subprocess.run([str(RUNTIME/'bin/wine'),'wineboot','-u'],env=env,stdout=f,stderr=subprocess.STDOUT,timeout=90,check=True)
        command=['/usr/bin/arch','-x86_64','/usr/bin/env',f'DYLD_LIBRARY_PATH={dyld}',f'DYLD_FALLBACK_LIBRARY_PATH={fallback}','DYLD_PRINT_LIBRARIES=1',str(RUNTIME/'bin/wine'),str(out/'slow-visual.exe')]
        start=time.monotonic()
        with (out/'runtime.log').open('w') as f: proc=subprocess.Popen(command,cwd=out,env=env,stdout=f,stderr=subprocess.STDOUT)
        log=out/'slow-visual_d3d11.log'; deadline=start+25; first_active=None
        while time.monotonic()<deadline:
            caps,commits,retires,_=active(log)
            if commits>retires and commits: first_active=time.monotonic(); break
            if proc.poll() is not None: break
            time.sleep(.05)
        before=None; taskkill_rc=None; elapsed=None
        if first_active:
            time.sleep(offset); elapsed=round(time.monotonic()-start,3); before=active(log)[:3]
            kill=['/usr/bin/arch','-x86_64','/usr/bin/env',f'DYLD_LIBRARY_PATH={dyld}',f'DYLD_FALLBACK_LIBRARY_PATH={fallback}','DYLD_PRINT_LIBRARIES=1',str(RUNTIME/'bin/wine'),'taskkill.exe','/F','/IM','slow-visual.exe']
            with (out/'taskkill.log').open('w') as f: taskkill_rc=subprocess.run(kill,cwd=out,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=15,check=False).returncode
        try: rc=proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rc='TIMEOUT'; proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        caps,commits,retires,logtext=active(log); runtime=(out/'runtime.log').read_text(errors='replace')
        result={**manifest,'first_active_commit_observed':first_active is not None,'elapsed_at_taskkill_seconds':elapsed,'active_counts_at_taskkill':before,'taskkill_returncode':taskkill_rc,'wine_client_returncode':rc,'captured_after_exit':caps,'committed_after_exit':commits,'job_retired_after_exit':retires,'normal_device_shutdown':'DeviceShutdown: normal' in logtext,'provider_shutdown':'MetalInterop: native provider resources released' in logtext,'provider_ready':'MetalInterop: provider ready' in logtext,'source_present_count':logtext.count('InternalWSI: source-present app='),'timeout_or_hang':rc=='TIMEOUT','runtime_termination_text':'terminated' in runtime.lower()}
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n'); all_results.append(result)
        clean={}
        for key,arg in [('kill','-k'),('wait','-w')]:
            try:
                c=subprocess.run([str(RUNTIME/'bin/wineserver'),arg],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=20,check=False); clean[key]={'returncode':c.returncode,'output':c.stdout[-1000:]}
            except Exception as e: clean[key]={'error':repr(e)}
        safe=Path('/Users/nima/Library/Caches/FGMetalStep11D-R')
        if prefix.parent==safe and prefix.name.startswith('step11d1-process-offset-') and prefix.exists() and not prefix.is_symlink(): shutil.rmtree(prefix); clean['prefix_removed']=True
        else: clean['prefix_removed']=False
        (out/'prefix-cleanup.json').write_text(json.dumps(clean,indent=2)+'\n')
    (BASE/'sequence-results.json').write_text(json.dumps(all_results,indent=2)+'\n')
    print(json.dumps(all_results,indent=2))
if __name__=='__main__': main()
