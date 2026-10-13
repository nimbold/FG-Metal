"""Bounded x86_64 observation proxy compile with durable command/identity evidence."""
import hashlib,json,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[2]
evidence=Path(__file__).resolve().parent
source=root/'trace_loader.mm'
output=root/'control/libvulkan-trace.dylib'
command=['xcrun','--sdk','macosx','clang++','-std=c++17','-dynamiclib','-arch','x86_64','-fblocks','-I','/Users/nima/Library/Caches/FGMetalStep11C1R/DXVK-MacOS/include/vulkan/include','-Wl,-install_name,@rpath/libfgmetal-vulkan-trace-proxy.dylib','-o',str(output),str(source)]
for framework in ['AppKit','Metal','QuartzCore','Foundation']: command += ['-framework',framework]
command+=['-lobjc']
with (evidence/'proxy-build.log').open('w') as log:
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
record={'command':command,'returncode':result.returncode,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest() if result.returncode==0 else None}
(evidence/'proxy-build.json').write_text(json.dumps(record,indent=2)+'\n')
raise SystemExit(result.returncode)
