import AppKit
import CoreGraphics
import Darwin
import Foundation

private let expectedWindowTitle = "ELDEN RING™"
private let expectedWindowWidth: CGFloat = 1470
private let expectedWindowHeight: CGFloat = 956
private let expectedGameArgument = "/Users/nima/Games/Elden Ring/Game/eldenring.exe"
private let mouseStepCount = 10
private let mouseDeltaPerStep: Int64 = 8
private let mouseStepDelay: useconds_t = 25_000
private let movementHold: useconds_t = 300_000
private let cameraStreamEventLimit = 18
private let cameraStreamDeltaPerEvent: Int64 = 24
private let cameraStreamDelay: useconds_t = 4_000

private func fail(_ reason: String) -> Never {
    fputs("R108_INPUT_TO_GAME=BLOCKED reason=\(reason)\n", stderr)
    exit(2)
}

private var firstEventNs: UInt64 = 0
private var lastEventNs: UInt64 = 0

private func monotonicNs() -> UInt64? {
    var value = timespec()
    guard clock_gettime(CLOCK_MONOTONIC, &value) == 0 else { return nil }
    return UInt64(value.tv_sec) * 1_000_000_000 + UInt64(value.tv_nsec)
}

private func processArguments(_ pid: pid_t) -> [String]? {
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, Int32(pid)]
    var size = 0
    let sizeStatus = mib.withUnsafeMutableBufferPointer { mibPointer in
        sysctl(mibPointer.baseAddress, u_int(mibPointer.count), nil, &size, nil, 0)
    }
    guard sizeStatus == 0, size > MemoryLayout<Int32>.size else { return nil }

    var bytes = [UInt8](repeating: 0, count: size)
    let readStatus = mib.withUnsafeMutableBufferPointer { mibPointer in
        bytes.withUnsafeMutableBytes { bytePointer in
            sysctl(mibPointer.baseAddress, u_int(mibPointer.count),
                   bytePointer.baseAddress, &size, nil, 0)
        }
    }
    guard readStatus == 0, size >= MemoryLayout<Int32>.size else { return nil }
    let argumentCount = bytes.withUnsafeBytes { $0.load(as: Int32.self) }
    guard argumentCount > 0 else { return nil }

    var cursor = MemoryLayout<Int32>.size
    while cursor < size, bytes[cursor] != 0 { cursor += 1 } // executable path
    guard cursor < size else { return nil }
    cursor += 1
    while cursor < size, bytes[cursor] == 0 { cursor += 1 } // kernel padding

    var arguments: [String] = []
    for _ in 0..<argumentCount {
        guard cursor < size else { return nil }
        let start = cursor
        while cursor < size, bytes[cursor] != 0 { cursor += 1 }
        guard cursor < size else { return nil }
        arguments.append(String(decoding: bytes[start..<cursor], as: UTF8.self))
        cursor += 1
    }
    return arguments
}

struct GameTarget {
    let pid: pid_t
    let bounds: CGRect
}

private enum InputFailure: Error {
    case keyboardEventAllocation
    case mouseEventAllocation
    case monotonicClockUnavailable
    case targetIdentityChanged
}

private var inputReleaseFailed = false

private func recordPostedEvent(pid: pid_t) throws {
    guard let postedAtNs = monotonicNs() else {
        throw InputFailure.monotonicClockUnavailable
    }
    if firstEventNs == 0 {
        firstEventNs = postedAtNs
        print("R108_INPUT_TO_GAME=FIRST_EVENT pid=\(pid) firstEventMonotonicNs=\(firstEventNs)")
        fflush(stdout)
    }
    lastEventNs = postedAtNs
}

private func validateTarget() -> GameTarget {
    let windows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
    var matches: [GameTarget] = []
    for info in windows {
        guard (info[kCGWindowName as String] as? String) == expectedWindowTitle,
              let owner = info[kCGWindowOwnerPID as String] as? Int,
              let rawBounds = info[kCGWindowBounds as String] as? [String: CGFloat],
              let x = rawBounds["X"], let y = rawBounds["Y"],
              let width = rawBounds["Width"], let height = rawBounds["Height"] else { continue }

        let pid = pid_t(owner)
        guard let app = NSRunningApplication(processIdentifier: pid),
              app.localizedName == "wine",
              app.executableURL?.lastPathComponent == "wine",
              abs(width - expectedWindowWidth) < 0.5,
              abs(height - expectedWindowHeight) < 0.5,
              let arguments = processArguments(pid),
              arguments.contains(expectedGameArgument) else { continue }
        matches.append(GameTarget(pid: pid,
                                  bounds: CGRect(x: x, y: y, width: width, height: height)))
    }

    guard matches.count == 1, let target = matches.first else {
        fail(matches.isEmpty ? "verified-elden-ring-window-not-found" : "ambiguous-elden-ring-window")
    }
    return target
}

private func postKey(_ keyCode: CGKeyCode, to pid: pid_t, down: Bool) throws {
    guard let event = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: down) else {
        throw InputFailure.keyboardEventAllocation
    }
    event.postToPid(pid)
    try recordPostedEvent(pid: pid)
}

private func postCameraSweep(_ sign: Int64, target: GameTarget) throws {
    for _ in 0..<mouseStepCount {
        guard let event = CGEvent(mouseEventSource: nil,
                                  mouseType: .mouseMoved,
                                  mouseCursorPosition: CGPoint(x: target.bounds.midX,
                                                               y: target.bounds.midY),
                                  mouseButton: .left) else {
            throw InputFailure.mouseEventAllocation
        }
        event.setIntegerValueField(.mouseEventDeltaX, value: sign * mouseDeltaPerStep)
        event.setIntegerValueField(.mouseEventDeltaY, value: 0)
        event.postToPid(target.pid)
        try recordPostedEvent(pid: target.pid)
        usleep(mouseStepDelay)
    }
}

private func postCameraStream(_ sign: Int64, target: GameTarget) throws {
    for eventIndex in 1...cameraStreamEventLimit {
        guard kill(target.pid, 0) == 0,
              NSWorkspace.shared.frontmostApplication?.processIdentifier == target.pid else {
            throw InputFailure.targetIdentityChanged
        }
        if eventIndex % 8 == 0 {
            guard let app = NSRunningApplication(processIdentifier: target.pid),
                  app.localizedName == "wine",
                  app.executableURL?.lastPathComponent == "wine",
                  let arguments = processArguments(target.pid),
                  arguments.contains(expectedGameArgument) else {
                throw InputFailure.targetIdentityChanged
            }
        }
        let delta = sign * cameraStreamDeltaPerEvent
        let offsetX = CGFloat(Int64(eventIndex) * delta)
        let location = CGPoint(x: target.bounds.midX + offsetX,
                               y: target.bounds.midY)
        guard let event = CGEvent(mouseEventSource: nil,
                                  mouseType: .mouseMoved,
                                  mouseCursorPosition: location,
                                  mouseButton: .left) else {
            throw InputFailure.mouseEventAllocation
        }
        event.setIntegerValueField(.mouseEventDeltaX, value: delta)
        event.setIntegerValueField(.mouseEventDeltaY, value: 0)
        event.post(tap: .cghidEventTap)
        try recordPostedEvent(pid: target.pid)
        print("R108_INPUT_TO_GAME=EVENT pid=\(target.pid) action=camera-right-stream eventIndex=\(eventIndex) postedMonotonicNs=\(lastEventNs) deltaPx=\(delta) positionX=\(Int(location.x)) route=CGEventPostFrontmostVerified")
        fflush(stdout)
        usleep(cameraStreamDelay)
    }
    print("R108_INPUT_TO_GAME=STREAM_BOUND_REACHED pid=\(target.pid) eventCount=\(cameraStreamEventLimit) totalCameraDeltaPx=\(cameraStreamEventLimit * Int(cameraStreamDeltaPerEvent))")
    fflush(stdout)
}

guard CommandLine.arguments.count == 2 || CommandLine.arguments.count == 3 else {
    fail("usage=send_bounded_input_to_game {check|still|camera-left|camera-right|camera-right-stream|character-forward|combined-right-forward} [expected-pid]")
}

let action = CommandLine.arguments[1]
let target = validateTarget()
if CommandLine.arguments.count == 3 {
    guard let expectedPID = pid_t(CommandLine.arguments[2]), expectedPID == target.pid else {
        fail("verified-window-owner-changed-before-input")
    }
}
guard CGPreflightPostEventAccess() else { fail("event-posting-not-already-authorized") }

if action == "check" {
    let checkTime = monotonicNs() ?? 0
    print("R108_INPUT_TO_GAME=READY pid=\(target.pid) executable=eldenring.exe window=\(expectedWindowTitle) bounds=\(target.bounds) monotonicNs=\(checkTime) route=CGEventPostToPid permission=preauthorized")
    exit(0)
}

guard let startNs = monotonicNs() else { fail("monotonic-clock-unavailable") }

private func withForwardKeyHeld(target: GameTarget,
                                body: () throws -> Void) throws {
    var keyIsDown = false
    defer {
        if keyIsDown {
            do {
                try postKey(13, to: target.pid, down: false)
                keyIsDown = false
            } catch {
                usleep(10_000)
                do {
                    try postKey(13, to: target.pid, down: false)
                    keyIsDown = false
                } catch {
                    inputReleaseFailed = true
                    fputs("R108_INPUT_RELEASE=FAILED pid=\(target.pid) key=W attempts=2\n", stderr)
                }
            }
        }
    }
    keyIsDown = true // arrange key-up even if timestamping fails after posting
    try postKey(13, to: target.pid, down: true) // W
    try body()
    try postKey(13, to: target.pid, down: false)
    keyIsDown = false
}

do {
    switch action {
    case "still":
        break
    case "camera-left":
        try postCameraSweep(-1, target: target)
    case "camera-right":
        try postCameraSweep(1, target: target)
    case "camera-right-stream":
        try postCameraStream(1, target: target)
    case "character-forward":
        try withForwardKeyHeld(target: target) {
            usleep(movementHold)
        }
    case "combined-right-forward":
        try withForwardKeyHeld(target: target) {
            try postCameraSweep(1, target: target)
            usleep(movementHold - mouseStepDelay * useconds_t(mouseStepCount))
        }
    default:
        fail("unknown-action")
    }
} catch {
    fail("input-action-failed:\(error)")
}
if inputReleaseFailed { fail("key-release-not-confirmed") }

guard let endNs = monotonicNs() else { fail("monotonic-clock-unavailable") }
let maxCameraDelta = action == "camera-right-stream"
    ? cameraStreamEventLimit * Int(cameraStreamDeltaPerEvent) : 80
let cameraDurationMs = firstEventNs == 0 ? 0 : (lastEventNs - firstEventNs) / 1_000_000
print("R108_INPUT_TO_GAME=POSTED action=\(action) pid=\(target.pid) conditionWindowTitle=\(expectedWindowTitle) startMonotonicNs=\(startNs) firstEventMonotonicNs=\(firstEventNs) lastEventMonotonicNs=\(lastEventNs) endMonotonicNs=\(endNs) maxCameraDeltaPx=\(maxCameraDelta) cameraDurationMs=\(cameraDurationMs) movementHoldMs=\(action == "camera-right-stream" ? 0 : 300) route=CGEventPostToPid permission=preauthorized")
