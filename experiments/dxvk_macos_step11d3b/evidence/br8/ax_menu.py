"""Read the public target accessibility menu; no UI mutation."""
from ax_api import *
pid=int(sys.argv[1]);app=A.AXUIElementCreateApplication(pid)
err,menu=attr(app,'AXMenuBar')
def tree(obj,depth=0):
 out=snapshot(obj)
 if depth<5:
  e,v=attr(obj,'AXChildren')
  if e==0 and v:
   out['children']=[tree(C.CFArrayGetValueAtIndex(v,i),depth+1) for i in range(min(100,C.CFArrayGetCount(v)))];C.CFRelease(v)
 return out
print(json.dumps({'pid':pid,'time_mach_ns':time.monotonic_ns(),'menu_error':err,'menu':tree(menu) if menu else None},indent=2))
if menu:C.CFRelease(menu)
C.CFRelease(app)
