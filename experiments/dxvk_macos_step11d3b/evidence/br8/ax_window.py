"""External public accessibility reads/actions for a PID-bound ordinary window.

Only normal UI attributes/actions are allowed; no target injection or rendering
property setters. Read-only mode verifies API access before scheduling actions.
"""
import ctypes,json,sys,time
A=ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
C=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
P=ctypes.c_void_p; I=ctypes.c_long
A.AXUIElementCreateApplication.argtypes=[ctypes.c_int];A.AXUIElementCreateApplication.restype=P
A.AXUIElementCopyAttributeValue.argtypes=[P,P,ctypes.POINTER(P)];A.AXUIElementCopyAttributeValue.restype=ctypes.c_int
A.AXUIElementSetAttributeValue.argtypes=[P,P,P];A.AXUIElementSetAttributeValue.restype=ctypes.c_int
A.AXUIElementPerformAction.argtypes=[P,P];A.AXUIElementPerformAction.restype=ctypes.c_int
A.AXUIElementIsAttributeSettable.argtypes=[P,P,ctypes.POINTER(ctypes.c_bool)];A.AXUIElementIsAttributeSettable.restype=ctypes.c_int
C.CFStringCreateWithCString.argtypes=[P,ctypes.c_char_p,ctypes.c_uint];C.CFStringCreateWithCString.restype=P
C.CFArrayGetCount.argtypes=[P];C.CFArrayGetCount.restype=I
C.CFArrayGetValueAtIndex.argtypes=[P,I];C.CFArrayGetValueAtIndex.restype=P
C.CFStringGetCString.argtypes=[P,ctypes.c_char_p,I,ctypes.c_uint];C.CFStringGetCString.restype=ctypes.c_bool
C.CFBooleanGetValue.argtypes=[P];C.CFBooleanGetValue.restype=ctypes.c_bool
C.CFGetTypeID.argtypes=[P];C.CFGetTypeID.restype=ctypes.c_ulong
C.CFStringGetTypeID.restype=ctypes.c_ulong;C.CFBooleanGetTypeID.restype=ctypes.c_ulong
C.CFRelease.argtypes=[P]
def string(s):return C.CFStringCreateWithCString(None,s.encode(),0x08000100)
def attr(obj,name):
 key=string(name);value=P();err=A.AXUIElementCopyAttributeValue(obj,key,ctypes.byref(value));C.CFRelease(key)
 return err,value.value
def scalar(v):
 if not v:return None
 typ=C.CFGetTypeID(v)
 if typ==C.CFStringGetTypeID():
  b=ctypes.create_string_buffer(4096);C.CFStringGetCString(v,b,len(b),0x08000100);return b.value.decode()
 if typ==C.CFBooleanGetTypeID():return bool(C.CFBooleanGetValue(v))
 return {'cf_type':typ}
def snapshot(obj):
 out={}
 for name in ['AXTitle','AXRole','AXSubrole','AXMain','AXFocused','AXMinimized']:
  err,v=attr(obj,name);out[name]={'error':err,'value':scalar(v)}
  if v:C.CFRelease(v)
 return out
pid=int(sys.argv[1]);mode=sys.argv[2] if len(sys.argv)>2 else 'read';start=time.monotonic_ns()
app=A.AXUIElementCreateApplication(pid);err,windows=attr(app,'AXWindows')
result={'pid':pid,'mode':mode,'start_mach_ns':start,'window_attribute_error':err,'windows':[]}
if err==0 and windows:
 count=C.CFArrayGetCount(windows);items=[C.CFArrayGetValueAtIndex(windows,i) for i in range(count)]
 result['windows']=[snapshot(w) for w in items]
 expected_title=sys.argv[3] if len(sys.argv)>3 else 'FG-Metal Step 11D.3-B Bare Vulkan'
 matches=[w for w in items if snapshot(w)['AXTitle']['value']==expected_title]
 if mode!='read':
  if len(matches)!=1:raise RuntimeError('exact target AX window title is not unique')
  w=matches[0]
  if mode=='frontmost':
   key=string('AXFrontmost');supported=ctypes.c_bool()
   check=A.AXUIElementIsAttributeSettable(app,key,ctypes.byref(supported))
   result['frontmost_settable']={'error':check,'value':supported.value}
   if check or not supported.value:raise RuntimeError('application AXFrontmost is not settable')
   val=P.in_dll(C,'kCFBooleanTrue').value
   result['action_error']=A.AXUIElementSetAttributeValue(app,key,val);C.CFRelease(key)
  elif mode in ('minimize','restore'):
   key=string('AXMinimized');val=P.in_dll(C,'kCFBooleanTrue' if mode=='minimize' else 'kCFBooleanFalse').value
   result['action_error']=A.AXUIElementSetAttributeValue(w,key,val);C.CFRelease(key)
  elif mode=='position':
   import math
   x,y=float(sys.argv[4]),float(sys.argv[5])
   if not all(math.isfinite(v) for v in (x,y)):raise RuntimeError('nonfinite position')
   class Point(ctypes.Structure):_fields_=[('x',ctypes.c_double),('y',ctypes.c_double)]
   A.AXValueCreate.argtypes=[ctypes.c_int,P];A.AXValueCreate.restype=P
   point=Point(x,y);value=A.AXValueCreate(1,ctypes.byref(point));key=string('AXPosition')
   result['requested_position']={'x':x,'y':y};result['action_error']=A.AXUIElementSetAttributeValue(w,key,value);C.CFRelease(key);C.CFRelease(value)
  elif mode=='raise':
   key=string('AXRaise');result['action_error']=A.AXUIElementPerformAction(w,key);C.CFRelease(key)
  else:raise RuntimeError('unapproved AX operation')
 C.CFRelease(windows)
C.CFRelease(app);result['end_mach_ns']=time.monotonic_ns();print(json.dumps(result,indent=2))
if err or result.get('action_error',0):raise SystemExit(1)
