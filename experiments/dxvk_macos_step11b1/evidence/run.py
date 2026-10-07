#!/usr/bin/env python3
import os,sys,time,json,hashlib,subprocess,shutil
from pathlib import Path
name=sys.argv[1]; root=Path('/tmp/fgmetal-step11b1'); out=Path(__file__).resolve().parent/name; out.mkdir(exist_ok=True)
wine=root/'runtime-frozen/wine/bin/wine'; prefix=root/('prefix-'+name)
diagnostic='--diagnostic' in sys.argv or '--fixed' in sys.argv
repository=name=='repository-smoke' or '--repository' in sys.argv
exe=root/('repo-smoke.exe' if repository else 'format-ab.exe')
if '--original' in sys.argv:exe=root/'preserved/d3d11_clear_window.exe'
if '--semantics' in sys.argv:exe=Path('/tmp/fgmetal-step11b/d3d11-semantics.exe')
dll = out.parent/'fixed-d3d11.dll' if '--fixed' in sys.argv else root/'build/src/d3d11/d3d11.dll' if diagnostic else Path('/tmp/fgmetal-step11b/app/d3d11.dll')
for target,source in {exe.name:exe,'d3d11.dll':dll,'dxgi.dll':Path('/tmp/fgmetal-step11b/app/dxgi.dll')}.items():shutil.copy2(source,out/target)
env=os.environ.copy()
for key in list(env):
 if key.startswith(('DXVK_','MVK_','MTL_','WINE','FG_','DYLD_')):env.pop(key)
env.update(WINEPREFIX=str(prefix),WINEDLLOVERRIDES='d3d11,dxgi=n,b',WINEDEBUG='+loaddll',DXVK_LOG_LEVEL='info',DXVK_LOG_PATH=str(out),DXVK_HUD='',DYLD_PRINT_LIBRARIES='1',FG_AUDIT_ENABLE='1',FG_AUDIT_RUN_SECONDS='60',FG_AUDIT_SKIP_RESIZE='1',FG_AB_BGRA='1' if name=='bgra8' or '--bgra' in sys.argv else '0')
preflight=Path(__file__).resolve().parents[3]/'experiments/storage/prefix_preflight.py'
subprocess.run([sys.executable,str(preflight),str(prefix),str(Path(__file__).resolve())],check=True)
if '--wsi-clear' in sys.argv:env['FG_STEP11B1_WSI_CLEAR']='1'
if '--constant-blitter' in sys.argv:env['FG_STEP11B1_CONSTANT_BLITTER']='1'
if '--known-source' in sys.argv:env['FG_STEP11B1_KNOWN_SOURCE']='1'
if '--trace-source' in sys.argv:env['FG_STEP11B1_TRACE_SOURCE']='1'
if '--flush-source' in sys.argv:env['FG_STEP11B1_FLUSH_SOURCE']='1'
if '--seconds30' in sys.argv:env['FG_AUDIT_RUN_SECONDS']='30'
if '--lifecycle' in sys.argv:env.update(FG_AUDIT_RUN_SECONDS='65',FG_AUDIT_SKIP_RESIZE='0',FG_AUDIT_LIFECYCLE_TEST='1')
if '--semantics' in sys.argv:env['FG_AUDIT_WAITABLE']='1'
if '--debug' in sys.argv:env.update(MTL_DEBUG_LAYER='1',MVK_CONFIG_DEBUG='1')
with (out/'prefix-init.log').open('w') as log:subprocess.run([str(wine),'wineboot','-u'],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=60)
print('START '+name,flush=True); start=time.monotonic()
with (out/'runtime.log').open('w') as log:
 p=subprocess.Popen([str(wine),str(out/exe.name)]+(['3600'] if repository else []),cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT)
 try:status=p.wait(timeout=100)
 except subprocess.TimeoutExpired:
  p.terminate()
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:p.kill();p.wait()
  status='TIMEOUT'
result={'name':name,'exit_status':status,'wall_seconds':time.monotonic()-start,'visual':'VISUAL CHECK REQUIRED','environment':{k:v for k,v in env.items() if k.startswith(('DXVK_','WINE','FG_','DYLD_','MVK_','MTL_'))},'hashes':{p.name:hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in out.iterdir() if p.suffix in ['.exe','.dll']}}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
