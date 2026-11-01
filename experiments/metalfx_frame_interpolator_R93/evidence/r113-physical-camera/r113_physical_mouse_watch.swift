import AppKit
import CoreGraphics
import Darwin
import Foundation

private func monotonicNs() -> UInt64 {
    var value = timespec()
    clock_gettime(CLOCK_MONOTONIC, &value)
    return UInt64(value.tv_sec) * 1_000_000_000 + UInt64(value.tv_nsec)
}

private func gameWindowBounds(pid: pid_t) -> CGRect? {
    let windows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
    for info in windows {
        guard (info[kCGWindowOwnerPID as String] as? Int) == Int(pid),
              (info[kCGWindowName as String] as? String) == "ELDEN RING™",
              let raw = info[kCGWindowBounds as String] as? [String: CGFloat],
              let x = raw["X"], let y = raw["Y"],
              let width = raw["Width"], let height = raw["Height"] else { continue }
        return CGRect(x: x, y: y, width: width, height: height)
    }
    return nil
}

private func topmostWindowOwner(at point: CGPoint) -> pid_t {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    for info in windows {
        guard let raw = info[kCGWindowBounds as String] as? [String: CGFloat],
              let x = raw["X"], let y = raw["Y"],
              let width = raw["Width"], let height = raw["Height"],
              CGRect(x: x, y: y, width: width, height: height).contains(point),
              (info[kCGWindowAlpha as String] as? CGFloat ?? 1) > 0.01,
              let owner = info[kCGWindowOwnerPID as String] as? Int else { continue }
        return pid_t(owner)
    }
    return -1
}

private var monitoredPid: pid_t = 0
private var monitoredBounds = CGRect.zero

guard CommandLine.arguments.count == 2, let rawPid = pid_t(CommandLine.arguments[1]) else {
    fputs("R113_MOUSE_MONITOR=BLOCKED reason=usage-pid-required\n", stderr)
    exit(2)
}

let targetPid = rawPid
guard CGPreflightListenEventAccess() else {
    fputs("R113_MOUSE_MONITOR=BLOCKED reason=input-monitoring-not-already-authorized\n", stderr)
    exit(5)
}
guard let bounds = gameWindowBounds(pid: targetPid) else {
    fputs("R113_MOUSE_MONITOR=BLOCKED reason=exact-elden-ring-window-not-found\n", stderr)
    exit(2)
}
monitoredPid = targetPid
monitoredBounds = bounds

let listenedTypes: [CGEventType] = [.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged]
let mask = listenedTypes.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << CGEventMask($1.rawValue)) }
let callback: CGEventTapCallBack = { _, eventType, event, _ in
    if eventType == .tapDisabledByTimeout || eventType == .tapDisabledByUserInput {
        return Unmanaged.passUnretained(event)
    }
    guard eventType == .mouseMoved || eventType == .leftMouseDragged ||
          eventType == .rightMouseDragged || eventType == .otherMouseDragged else {
        return Unmanaged.passUnretained(event)
    }
    let point = event.location
    let insideGameWindow = monitoredBounds.contains(point) ? 1 : 0
    let frontmostPid = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
    let topWindowPid = topmostWindowOwner(at: point)
    let dx = event.getIntegerValueField(.mouseEventDeltaX)
    let dy = event.getIntegerValueField(.mouseEventDeltaY)
    print("R113_PHYSICAL_MOUSE_EVENT pid=\(monitoredPid) monotonicNs=\(monotonicNs()) x=\(Int(point.x)) y=\(Int(point.y)) deltaX=\(dx) deltaY=\(dy) withinGameWindow=\(insideGameWindow) frontmostPid=\(frontmostPid) topWindowPid=\(topWindowPid) eventType=\(eventType.rawValue)")
    fflush(stdout)
    return Unmanaged.passUnretained(event)
}

guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                  place: .headInsertEventTap,
                                  options: .listenOnly,
                                  eventsOfInterest: mask,
                                  callback: callback,
                                  userInfo: nil) else {
    fputs("R113_MOUSE_MONITOR=BLOCKED reason=physical-event-tap-unavailable\n", stderr)
    exit(3)
}

guard let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0) else {
    fputs("R113_MOUSE_MONITOR=BLOCKED reason=runloop-source-unavailable\n", stderr)
    exit(4)
}
CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)
let frontmostPid = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
let centerWindowPid = topmostWindowOwner(at: CGPoint(x: bounds.midX, y: bounds.midY))
print("R113_MOUSE_MONITOR=READY pid=\(targetPid) bounds=(\(bounds.origin.x),\(bounds.origin.y),\(bounds.width),\(bounds.height)) frontmostPid=\(frontmostPid) centerTopWindowPid=\(centerWindowPid) mode=listen-only syntheticEventsSent=0")
fflush(stdout)
CFRunLoopRun()
