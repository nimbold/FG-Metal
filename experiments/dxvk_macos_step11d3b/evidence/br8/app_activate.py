"""External public NSRunningApplication activation for authorized window-state tests.

No code is injected into Wine. This is ordinary application focus, not a layer,
renderer or Vulkan mutation. User explicitly permits public AppKit controls.
"""
import ctypes,json,sys,time
objc=ctypes.CDLL('/usr/lib/libobjc.A.dylib');ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
objc.objc_getClass.argtypes=[ctypes.c_char_p];objc.objc_getClass.restype=ctypes.c_void_p
objc.sel_registerName.argtypes=[ctypes.c_char_p];objc.sel_registerName.restype=ctypes.c_void_p
addr=ctypes.cast(objc.objc_msgSend,ctypes.c_void_p).value
object_call=ctypes.CFUNCTYPE(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int)(addr)
get_object=ctypes.CFUNCTYPE(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p)(addr)
get_string=ctypes.CFUNCTYPE(ctypes.c_char_p,ctypes.c_void_p,ctypes.c_void_p)(addr)
bool_call=ctypes.CFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong)(addr)
pid=int(sys.argv[1]);start=time.monotonic_ns()
app=object_call(objc.objc_getClass(b'NSRunningApplication'),objc.sel_registerName(b'runningApplicationWithProcessIdentifier:'),pid)
if not app: raise RuntimeError('exact running application PID unavailable')
bundle=get_object(app,objc.sel_registerName(b'bundleIdentifier'))
bundle_id=get_string(bundle,objc.sel_registerName(b'UTF8String')).decode() if bundle else None
if len(sys.argv)>2 and bundle_id!=sys.argv[2]: raise RuntimeError('running application bundle identity mismatch')
accepted=bool_call(app,objc.sel_registerName(b'activateWithOptions:'),2)
row={'target_pid':pid,'bundle_identifier':bundle_id,'api':'NSRunningApplication.activateWithOptions(NSApplicationActivateIgnoringOtherApps)','accepted':accepted,'start_mach_ns':start,'end_mach_ns':time.monotonic_ns(),'renderer_injected':False,'layer_mutated':False,'requires_target_telemetry_attainment':True}
print(json.dumps(row))
if not accepted: raise SystemExit(1)
