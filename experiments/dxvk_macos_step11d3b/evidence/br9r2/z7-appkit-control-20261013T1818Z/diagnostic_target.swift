import AppKit
import CoreGraphics
import Darwin
import Dispatch
import Foundation

private let targetBoundsCG = CGRect(x: 68, y: 62, width: 640, height: 392)
private let expectedDisplayID: UInt32 = 1
private let visibleBit = UInt64(NSWindow.OcclusionState.visible.rawValue)

private func utcNow() -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: Date())
}

private func rectJSON(_ rect: NSRect) -> [String: Double] {
    ["x": Double(rect.origin.x), "y": Double(rect.origin.y),
     "width": Double(rect.size.width), "height": Double(rect.size.height)]
}

private func displayNumber(_ screen: NSScreen?) -> UInt32? {
    guard let screen,
          let value = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
        return nil
    }
    return value.uint32Value
}

private func appKitFrame(for bounds: CGRect, displayID: UInt32) -> NSRect? {
    for screen in NSScreen.screens {
        guard displayNumber(screen) == displayID,
              let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
            continue
        }
        let displayBounds = CGDisplayBounds(CGDirectDisplayID(number.uint32Value))
        guard displayBounds.width > 0, displayBounds.height > 0,
              displayBounds.contains(CGPoint(x: bounds.midX, y: bounds.midY)) else {
            continue
        }
        let scaleX = screen.frame.width / displayBounds.width
        let scaleY = screen.frame.height / displayBounds.height
        return NSRect(x: screen.frame.minX + (bounds.minX - displayBounds.minX) * scaleX,
                      y: screen.frame.minY + (displayBounds.maxY - bounds.maxY) * scaleY,
                      width: bounds.width * scaleX, height: bounds.height * scaleY)
    }
    return nil
}

private final class AppKitTarget {
    private let application: NSApplication
    private let window: NSWindow
    private let output: FileHandle
    private var timer: Timer?
    private var inputSource: DispatchSourceRead?
    private var inputBuffer = Data()

    init(title: String, outputURL: URL) throws {
        guard !FileManager.default.fileExists(atPath: outputURL.path) else {
            throw NSError(domain: "Z7AppKitTarget", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "telemetry output already exists"])
        }
        guard FileManager.default.createFile(atPath: outputURL.path, contents: nil),
              let outputHandle = try? FileHandle(forWritingTo: outputURL) else {
            throw NSError(domain: "Z7AppKitTarget", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "cannot create telemetry output"])
        }
        guard let frame = appKitFrame(for: targetBoundsCG, displayID: expectedDisplayID) else {
            try? outputHandle.close()
            throw NSError(domain: "Z7AppKitTarget", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "expected display does not contain target geometry"])
        }

        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.finishLaunching()
        let candidate = NSWindow(contentRect: frame, styleMask: [.titled],
                                 backing: .buffered, defer: false)
        candidate.title = title
        candidate.level = .normal
        candidate.backgroundColor = NSColor.systemGreen
        candidate.isOpaque = true
        candidate.alphaValue = 1
        candidate.hasShadow = false
        candidate.isMovable = false
        candidate.isReleasedWhenClosed = false
        candidate.setFrame(frame, display: true)
        app.activate(ignoringOtherApps: true)
        candidate.makeKeyAndOrderFront(nil)
        candidate.displayIfNeeded()

        application = app
        window = candidate
        output = outputHandle

        try record(trigger: "initial")
        emit(["event": "ready", "pid": Int(getpid()), "title": title,
              "windowNumber": window.windowNumber,
              "targetBoundsCG": ["x": targetBoundsCG.minX, "y": targetBoundsCG.minY,
                                 "width": targetBoundsCG.width, "height": targetBoundsCG.height],
              "windowFrameAppKit": rectJSON(window.frame),
              "screenID": displayNumber(window.screen) as Any? ?? NSNull(),
              "isActive": application.isActive, "isKey": window.isKeyWindow,
              "isMain": window.isMainWindow])

        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self else { return }
            do { try self.record(trigger: "periodic") }
            catch {
                fputs("telemetry write failed: \(error)\n", stderr)
                self.application.terminate(nil)
            }
        }
        let source = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
        source.setEventHandler { [weak self] in self?.readCommands() }
        source.resume()
        inputSource = source
    }

    func run() { application.run() }

    private func readCommands() {
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = bytes.withUnsafeMutableBytes { buffer in
            Darwin.read(STDIN_FILENO, buffer.baseAddress, buffer.count)
        }
        guard count > 0 else {
            inputSource?.cancel()
            application.stop(nil)
            return
        }
        inputBuffer.append(contentsOf: bytes.prefix(count))
        while let newline = inputBuffer.firstIndex(of: 0x0A) {
            let line = inputBuffer[..<newline]
            inputBuffer.removeSubrange(...newline)
            guard let command = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                  command["command"] as? String == "quit" else {
                emit(["event": "command", "status": "FAIL", "reason": "unsupported command"])
                continue
            }
            do { try record(trigger: "quit") }
            catch { fputs("final telemetry write failed: \(error)\n", stderr) }
            window.orderOut(nil)
            emit(["event": "command", "command": "quit", "status": "PASS"])
            timer?.invalidate()
            application.stop(nil)
        }
    }

    private func record(trigger: String) throws {
        let timestamp = DispatchTime.now().uptimeNanoseconds
        let state = UInt64(window.occlusionState.rawValue)
        let row: [String: Any] = [
            "event": "window_telemetry", "trigger": trigger,
            "timestampUTC": utcNow(), "timestampUptimeNS": timestamp,
            "pid": Int(getpid()), "windowNumber": window.windowNumber,
            "windowFrameAppKit": rectJSON(window.frame),
            "screenID": displayNumber(window.screen) as Any? ?? NSNull(),
            "isActive": application.isActive, "isKey": window.isKeyWindow,
            "isMain": window.isMainWindow, "isVisible": window.isVisible,
            "isMiniaturized": window.isMiniaturized,
            "occlusionState": state, "visibleBitSet": (state & visibleBit) != 0,
            "alpha": Double(window.alphaValue), "isOpaque": window.isOpaque,
        ]
        let data = try JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
        try output.write(contentsOf: data)
        try output.write(contentsOf: Data([0x0A]))
    }

    private func emit(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

@main
private enum Main {
    static func main() {
        guard CommandLine.arguments.count == 3 else {
            fputs("usage: z7-appkit-target <unique-window-title> <telemetry-jsonl>\n", stderr)
            exit(64)
        }
        do {
            let target = try AppKitTarget(title: CommandLine.arguments[1],
                                          outputURL: URL(fileURLWithPath: CommandLine.arguments[2]))
            target.run()
        } catch {
            fputs("z7 AppKit target failed: \(error)\n", stderr)
            exit(1)
        }
    }
}
