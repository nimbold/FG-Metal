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
