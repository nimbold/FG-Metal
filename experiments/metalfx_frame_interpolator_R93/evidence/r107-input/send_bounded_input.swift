import AppKit
import CoreGraphics
import Foundation

let highballBundleID = "app.highball.Highball"
let mouseStepCount = 10
let mouseDeltaPerStep: Int64 = 8
let mouseStepDelay: useconds_t = 25_000
let movementHold: useconds_t = 300_000

func fail(_ message: String) -> Never {
    fputs("R107_INPUT=BLOCKED reason=\(message)\n", stderr)
    exit(2)
}

func postKey(_ keyCode: CGKeyCode, down: Bool) {
    guard let event = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: down) else {
        fail("keyboard-event-allocation")
    }
    event.post(tap: .cghidEventTap)
}

func holdKey(_ keyCode: CGKeyCode) {
    postKey(keyCode, down: true)
    defer { postKey(keyCode, down: false) }
    usleep(movementHold)
}

func moveCamera(_ sign: Int64) {
    for _ in 0..<mouseStepCount {
        let position = CGEvent(source: nil)?.location ?? .zero
        guard let event = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                                  mouseCursorPosition: position, mouseButton: .left) else {
            fail("mouse-event-allocation")
        }
        event.setIntegerValueField(.mouseEventDeltaX, value: sign * mouseDeltaPerStep)
        event.post(tap: .cghidEventTap)
        usleep(mouseStepDelay)
    }
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: send_bounded_input {check|camera-left|camera-right|forward|back|strafe-left|strafe-right|combined-left-forward|combined-right-forward}")
}

let action = CommandLine.arguments[1]
let frontmostBundleID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "<none>"
guard frontmostBundleID == highballBundleID else {
    fail("Highball-not-frontmost bundle=\(frontmostBundleID)")
}

guard CGPreflightPostEventAccess() else {
    fail("CGEvent-posting-not-already-authorized")
}

if action == "check" {
    print("R107_INPUT=READY mechanism=Quartz-CGEvent posting=preauthorized frontmost=\(frontmostBundleID) actions=bounded-whitelist")
    exit(0)
}

switch action {
case "camera-left":
    moveCamera(-1)
case "camera-right":
    moveCamera(1)
case "forward":
    holdKey(13) // W
case "back":
    holdKey(1) // S
case "strafe-left":
    holdKey(0) // A
case "strafe-right":
    holdKey(2) // D
case "combined-left-forward":
    postKey(13, down: true)
    defer { postKey(13, down: false) }
    moveCamera(-1)
case "combined-right-forward":
    postKey(13, down: true)
    defer { postKey(13, down: false) }
    moveCamera(1)
default:
    fail("unknown-action")
}

print("R107_INPUT=POSTED action=\(action) frontmost=\(frontmostBundleID) maxMouseDelta=80px mouseWindow=250ms movementHold=300ms")
