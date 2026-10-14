import AppKit
import CoreGraphics
import Darwin
import Dispatch
import Foundation

private final class DiagnosticWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

private final class TargetController {
    private let application = NSApplication.shared
    private let title: String
    private let window: DiagnosticWindow
    private var inputSource: DispatchSourceRead?
    private var inputBuffer = Data()

    init(title: String) throws {
        self.title = title
        application.setActivationPolicy(.regular)
        guard let screen = NSScreen.main ?? NSScreen.screens.first,
              screen.visibleFrame.width >= 500, screen.visibleFrame.height >= 500 else {
            throw NSError(domain: "DiagnosticTarget", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no primary screen large enough for diagnostic geometry"])
        }
        let frame = NSRect(x: screen.visibleFrame.minX + 100,
                           y: screen.visibleFrame.minY + 100,
                           width: 300, height: 300)
        window = DiagnosticWindow(contentRect: frame, styleMask: [.titled],
                                  backing: .buffered, defer: false)
        window.title = title
        window.level = .normal
        window.backgroundColor = NSColor.systemGreen
        window.isOpaque = true
        window.alphaValue = 1
        window.hasShadow = false
        window.isMovable = false
        window.isReleasedWhenClosed = false
        application.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.displayIfNeeded()

        let source = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
        source.setEventHandler { [weak self] in self?.readCommands() }
        source.resume()
        inputSource = source
        write(["event": "ready", "pid": Int(getpid()), "title": title,
               "windowNumber": window.windowNumber,
               "applicationIsActive": application.isActive,
               "windowIsKey": window.isKeyWindow,
               "windowFrameAppKit": rectJSON(window.frame),
               "screens": screenRows()])
    }

    func run() { application.run() }

    private func readCommands() {
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = bytes.withUnsafeMutableBytes { buffer in
            Darwin.read(STDIN_FILENO, buffer.baseAddress, buffer.count)
        }
        if count <= 0 {
            inputSource?.cancel()
            application.stop(nil)
            return
        }
        inputBuffer.append(contentsOf: bytes.prefix(count))
        while let newline = inputBuffer.firstIndex(of: 0x0A) {
            let line = inputBuffer[..<newline]
            inputBuffer.removeSubrange(...newline)
            guard let command = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
            else {
                write(["event": "command", "status": "FAIL", "reason": "invalid JSON command"])
                continue
            }
            handle(command)
        }
    }

    private func handle(_ command: [String: Any]) {
        let action = command["command"] as? String ?? ""
        switch action {
        case "wine_geometry":
            let cg = CGRect(x: 68, y: 62, width: 640, height: 392)
            guard let converted = appKitFrame(for: cg) else {
                write(["event": "command", "command": action, "status": "FAIL",
                       "reason": "retained Wine target bounds do not intersect a connected screen",
                       "targetBoundsCG": rectJSON(cg), "screens": screenRows()])
                return
            }
            window.setFrame(converted, display: true)
            window.displayIfNeeded()
            write(["event": "command", "command": action, "status": "PASS",
                   "targetBoundsCGRequested": rectJSON(cg),
                   "windowFrameAppKit": rectJSON(window.frame),
                   "windowNumber": window.windowNumber, "screens": screenRows()])
        case "status":
            write(["event": "command", "command": action, "status": "PASS",
                   "windowFrameAppKit": rectJSON(window.frame),
                   "windowNumber": window.windowNumber, "screens": screenRows()])
        case "quit":
            window.orderOut(nil)
            write(["event": "command", "command": action, "status": "PASS"])
            application.terminate(nil)
        default:
            write(["event": "command", "command": action, "status": "FAIL",
                   "reason": "unsupported diagnostic target command"])
        }
    }

    private func appKitFrame(for bounds: CGRect) -> NSRect? {
        for screen in NSScreen.screens {
            guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { continue }
            let displayBounds = CGDisplayBounds(CGDirectDisplayID(number.uint32Value))
            guard displayBounds.width > 0, displayBounds.height > 0,
                  displayBounds.contains(CGPoint(x: bounds.midX, y: bounds.midY)) else { continue }
            let scaleX = screen.frame.width / displayBounds.width
            let scaleY = screen.frame.height / displayBounds.height
            let x = screen.frame.minX + (bounds.minX - displayBounds.minX) * scaleX
            let y = screen.frame.minY + (displayBounds.maxY - bounds.maxY) * scaleY
            return NSRect(x: x, y: y, width: bounds.width * scaleX, height: bounds.height * scaleY)
        }
        return nil
    }

    private func screenRows() -> [[String: Any]] {
        NSScreen.screens.map { screen in
            let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
            let cg = id.map { CGDisplayBounds($0) }
            return ["displayID": id.map { Int($0) } as Any? ?? NSNull(),
                    "name": screen.localizedName,
                    "isMainScreen": screen == NSScreen.main,
                    "frameAppKit": rectJSON(screen.frame),
                    "visibleFrameAppKit": rectJSON(screen.visibleFrame),
                    "boundsCG": cg.map(rectJSON) as Any? ?? NSNull(),
                    "backingScaleFactor": screen.backingScaleFactor]
        }
    }

    private func rectJSON(_ rect: CGRect) -> [String: Double] {
        ["x": rect.origin.x, "y": rect.origin.y,
         "width": rect.width, "height": rect.height,
         "minX": rect.minX, "minY": rect.minY,
         "maxX": rect.maxX, "maxY": rect.maxY]
    }

    private func write(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

@main
private struct DiagnosticTargetMain {
    static func main() throws {
        guard CommandLine.arguments.count == 2 else {
            fputs("usage: diagnostic-target <unique-window-title>\n", stderr)
            exit(64)
        }
        try TargetController(title: CommandLine.arguments[1]).run()
    }
}
