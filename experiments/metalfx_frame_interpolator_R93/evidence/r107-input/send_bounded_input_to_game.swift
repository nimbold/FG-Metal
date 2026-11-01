import AppKit
import CoreGraphics
import Foundation

// Direct-to-process input is used only because Highball and the game window
// are on different Spaces. No Accessibility permissions are requested.
let targetPID: pid_t = 59760
let expectedWindowTitle = "ELDEN RING™"
let windowWidth: CGFloat = 1470
let windowHeight: CGFloat = 956
let mouseStepCount = 10
let mouseDeltaPerStep: Int64 = 8
let mouseStepDelay: useconds_t = 25_000
let movementHold: useconds_t = 300_000

func fail(_ message: String) -> Never {
    fputs("R107_INPUT_TO_GAME=BLOCKED reason=\(message)\n", stderr)
    exit(2)
}

func validateTarget() -> CGRect {
    guard let app = NSRunningApplication(processIdentifier: targetPID),
          app.localizedName == "wine",
          app.executableURL?.lastPathComponent == "wine" else {
        fail("unexpected-target-process")
    }

    guard CGPreflightPostEventAccess() else { fail("CGEvent-posting-not-already-authorized") }

    let windows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
    for info in windows {
        guard (info[kCGWindowOwnerPID as String] as? Int) == Int(targetPID),
              (info[kCGWindowName as String] as? String) == expectedWindowTitle,
              let bounds = info[kCGWindowBounds as String] as? [String: CGFloat],
              let width = bounds["Width"], let height = bounds["Height"],
              let x = bounds["X"], let y = bounds["Y"] else { continue }
        guard width == windowWidth && height == windowHeight else {
            fail("unexpected-game-window-size width=\(width) height=\(height)")
        }
        return CGRect(x: x, y: y, width: width, height: height)
    }
    fail("exact-game-window-not-found")
}

func postKey(_ keyCode: CGKeyCode, down: Bool) {
    guard let event = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: down) else {
        fail("keyboard-event-allocation")
    }
    event.postToPid(targetPID)
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: send_bounded_input_to_game {check|camera-left|camera-right|back}")
}

let action = CommandLine.arguments[1]
let bounds = validateTarget()
if action == "check" {
    print("R107_INPUT_TO_GAME=READY pid=\(targetPID) executable=eldenring.exe window=\(expectedWindowTitle) windowBounds=\(bounds) mechanism=Quartz-CGEventPostToPid permission=preauthorized")
    exit(0)
}

switch action {
case "camera-left", "camera-right":
    let sign: Int64 = action == "camera-left" ? -1 : 1
    let center = CGPoint(x: bounds.midX, y: bounds.midY)
    for _ in 0..<mouseStepCount {
        guard let event = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                                  mouseCursorPosition: center, mouseButton: .left) else {
            fail("mouse-event-allocation")
        }
        event.setIntegerValueField(.mouseEventDeltaX, value: sign * mouseDeltaPerStep)
        event.postToPid(targetPID)
        usleep(mouseStepDelay)
    }
case "back":
    postKey(1, down: true) // S
    defer { postKey(1, down: false) }
    usleep(movementHold)
default:
    fail("unknown-action")
}

print("R107_INPUT_TO_GAME=POSTED action=\(action) pid=\(targetPID) window=\(expectedWindowTitle) maxMouseDelta=80px mouseWindow=250ms movementHold=300ms")
