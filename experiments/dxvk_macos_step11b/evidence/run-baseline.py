#!/usr/bin/env python3
import os,sys,subprocess,json,time,shutil,hashlib
from pathlib import Path
root=Path('/tmp/fgmetal-step11b')
name=sys.argv[1];duration=sys.argv[2];enhanced='--semantics' in sys.argv;waitable='--waitable' in sys.argv;lifecycle='--lifecycle' in sys.argv
old='--old' in sys.argv
runtime=Path('/tmp/fgmetal-dxvk-macos-audit-run/runtime/wine') if old else root/'runtime-candidate/wine'
prefix=root/('prefix-negative' if old else 'prefix')
out=Path(__file__).resolve().parent/name;out.mkdir(exist_ok=True)
exe=root/('d3d11-semantics.exe' if enhanced else 'app/d3d11_clear_window.exe')
shutil.copy2(exe,out/exe.name)
for dll in ['d3d11.dll','dxgi.dll']:shutil.copy2(root/'app'/dll,out/dll)
env=os.environ.copy();env.update(WINEPREFIX=str(prefix),WINEDLLOVERRIDES='d3d11,dxgi=n,b',WINEDEBUG='+loaddll',DXVK_LOG_LEVEL='info',DXVK_HUD='' if '--no-hud' in sys.argv else 'fps',DXVK_LOG_PATH=str(out),FG_AUDIT_ENABLE='1',FG_AUDIT_RUN_SECONDS=duration,FG_AUDIT_SKIP_RESIZE='0' if lifecycle else '1',FG_AUDIT_LIFECYCLE_TEST='1' if lifecycle else '0',FG_AUDIT_WAITABLE='1' if waitable else '0',DYLD_PRINT_LIBRARIES='1')
# Do not inject a loader/ICD into winevulkan: use the candidate's native payload.
preflight=Path(__file__).resolve().parents[3]/'experiments/storage/prefix_preflight.py'
subprocess.run([sys.executable,str(preflight),str(prefix),str(Path(__file__).resolve())],check=True)
start=time.monotonic()
with (out/'runtime.log').open('w') as log:
 p=subprocess.Popen([str(runtime/'bin/wine'),str(out/exe.name)],cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT)
 try:status=p.wait(timeout=int(duration)+45)
 except subprocess.TimeoutExpired:
  status='timeout';p.terminate()
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:p.kill();p.wait()
summary={'environment':{k:env.get(k) for k in ['WINEPREFIX','WINEDLLOVERRIDES','WINEDEBUG','DXVK_LOG_LEVEL','DXVK_HUD','DXVK_LOG_PATH','FG_AUDIT_ENABLE','FG_AUDIT_RUN_SECONDS','FG_AUDIT_SKIP_RESIZE','FG_AUDIT_LIFECYCLE_TEST','FG_AUDIT_WAITABLE','DYLD_PRINT_LIBRARIES','MTL_DEBUG_LAYER','MVK_CONFIG_DEBUG']},'runtime':str(runtime),'exit_status':status,'wall_seconds':time.monotonic()-start,'duration':int(duration),'enhanced':enhanced,'waitable':waitable,'lifecycle':lifecycle,'dll_hashes':{d:hashlib.sha256((out/d).read_bytes()).hexdigest() for d in ['d3d11.dll','dxgi.dll']}}
(out/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
sys.exit(0 if status==0 else 1)
