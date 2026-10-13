"""Public AX press of Finder's exact New Finder Window menu command."""
from ax_api import *
import subprocess
subprocess.run([sys.executable, str(__import__('pathlib').Path(__file__).with_name('app_activate.py')), '867', 'com.apple.finder'], check=True)
start = time.monotonic_ns()
app = A.AXUIElementCreateApplication(867)
window_error, windows = attr(app, 'AXWindows')
existing = []
if window_error == 0 and windows:
    existing = [snapshot(C.CFArrayGetValueAtIndex(windows, i)) for i in range(C.CFArrayGetCount(windows))]
    C.CFRelease(windows)
recent_count = sum(w['AXTitle']['value'] == 'Recents' for w in existing)
if recent_count == 1:
    print(json.dumps(dict(start_mach_ns=start, end_mach_ns=time.monotonic_ns(), action='reuse unique existing Recents window', action_error=0)))
    C.CFRelease(app)
    raise SystemExit(0)
if recent_count > 1:
    raise RuntimeError('existing Recents windows ambiguous')
err, menu = attr(app, 'AXMenuBar')
if err or not menu:
    raise RuntimeError('Finder menu unavailable')
matches = []
def visit(obj, depth=0):
    state = snapshot(obj)
    if state['AXTitle']['value'] == 'New Finder Window' and state['AXRole']['value'] == 'AXMenuItem':
        matches.append(obj)
    if depth < 5:
        error, children = attr(obj, 'AXChildren')
        if error == 0 and children:
            for i in range(min(100, C.CFArrayGetCount(children))):
                visit(C.CFArrayGetValueAtIndex(children, i), depth + 1)
            # Keep menu descendants retained until the action is complete.
visit(menu)
if len(matches) != 1:
    raise RuntimeError('New Finder Window menu item not unique')
key = string('AXPress')
error = A.AXUIElementPerformAction(matches[0], key)
C.CFRelease(key)
print(json.dumps(dict(start_mach_ns=start, end_mach_ns=time.monotonic_ns(), action='Finder New Finder Window AXPress', action_error=error)))
C.CFRelease(menu)
C.CFRelease(app)
raise SystemExit(1 if error else 0)
