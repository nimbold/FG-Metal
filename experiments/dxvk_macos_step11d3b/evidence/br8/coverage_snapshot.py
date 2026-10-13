"""External read-only public CoreGraphics geometry; does not activate/capture/mutate."""
import ctypes,json,plistlib,sys,time
from pathlib import Path
cg=ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
cg.CGWindowListCopyWindowInfo.argtypes=[ctypes.c_uint32,ctypes.c_uint32];cg.CGWindowListCopyWindowInfo.restype=ctypes.c_void_p
cf.CFPropertyListCreateData.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_long,ctypes.c_ulong,ctypes.c_void_p];cf.CFPropertyListCreateData.restype=ctypes.c_void_p
cf.CFDataGetLength.argtypes=[ctypes.c_void_p];cf.CFDataGetLength.restype=ctypes.c_long
cf.CFDataGetBytePtr.argtypes=[ctypes.c_void_p];cf.CFDataGetBytePtr.restype=ctypes.c_void_p
cf.CFRelease.argtypes=[ctypes.c_void_p]
cg.CGGetActiveDisplayList.argtypes=[ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
target=int(sys.argv[1]); start=time.monotonic_ns()
arr=cg.CGWindowListCopyWindowInfo(1|16,0)
if not arr: raise RuntimeError('CG window listing unavailable')
data=cf.CFPropertyListCreateData(None,arr,200,0,None)
if not data: raise RuntimeError('CG property list cannot serialize')
rows=plistlib.loads(ctypes.string_at(cf.CFDataGetBytePtr(data),cf.CFDataGetLength(data)))
cf.CFRelease(data);cf.CFRelease(arr)
n=ctypes.c_uint32(); displays=(ctypes.c_uint32*16)();ret=cg.CGGetActiveDisplayList(16,displays,ctypes.byref(n))
filtered=[{'front_to_back_rank':i,**r} for i,r in enumerate(rows) if r.get('kCGWindowOwnerPID')==target or r.get('kCGWindowOwnerName')=='Finder']
out={'start_mach_ns':start,'end_mach_ns':time.monotonic_ns(),'target_pid':target,'active_displays':list(displays)[:n.value] if ret==0 else None,'windows':filtered,'state_mutated':False,'extent_note':'CG window rectangles and rank do not alone prove opaque per-pixel coverage; pair with external UI visual observation.'}
if len(sys.argv)>2: Path(sys.argv[2]).write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out,indent=2))
