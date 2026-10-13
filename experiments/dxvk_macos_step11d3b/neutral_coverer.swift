import AppKit
import CoreGraphics
import Darwin
import Dispatch
import Foundation

private final class NeutralWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class SolidBlackView: NSView {
    override var isOpaque: Bool { true }

    override func viewDidMoveToSuperview() {
        super.viewDidMoveToSuperview()
        autoresizingMask = [.width, .height]
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill()
        dirtyRect.fill()
    }
}

private final class CovererController {
    private let application = NSApplication.shared
    private let window = NeutralWindow(
        contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
        styleMask: .borderless,
        backing: .buffered,
        defer: false
    )
    private var inputSource: DispatchSourceRead?
    private var inputBuffer = Data()
    private var targetPID: pid_t?
    private var targetTitle: String?
    private var isShown = false

    init() {
        application.setActivationPolicy(.accessory)
        window.level = .modalPanel
        window.backgroundColor = .black
        window.isOpaque = true
        window.alphaValue = 1.0
        window.ignoresMouseEvents = true
        window.hidesOnDeactivate = false
        window.hasShadow = false
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        let blackView = SolidBlackView(frame: window.contentRect(forFrameRect: window.frame))
        blackView.autoresizingMask = [.width, .height]
        window.contentView = blackView

        let source = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
        source.setEventHandler { [weak self] in self?.readCommands() }
        source.resume()
        inputSource = source

        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            guard let self, self.isShown else { return }
            _ = self.placeOverTarget()
        }
        writeResponse(["event": "ready", "pid": Int(getpid()), "status": "PASS"])
    }

    func run() {
        application.run()
    }

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
                writeResponse(["event": "command", "status": "FAIL", "reason": "invalid JSON command"])
                continue
            }
            handle(command)
        }
    }

    private func handle(_ command: [String: Any]) {
        let action = command["command"] as? String ?? ""
        switch action {
        case "target":
            guard let pidValue = command["pid"] as? Int, pidValue > 0,
                  let title = command["title"] as? String, !title.isEmpty else {
                writeResponse(["event": "command", "command": action, "status": "FAIL",
                               "reason": "target requires a positive pid and exact window title"])
                return
            }
            targetPID = pid_t(pidValue)
            targetTitle = title
            writeResponse(snapshot(command: action))
        case "show":
            isShown = true
            guard placeOverTarget() else {
                writeResponse(snapshot(command: action, failure: "target window or display geometry is unavailable"))
                return
            }
            window.orderFrontRegardless()
            window.displayIfNeeded()
            writeResponse(snapshot(command: action))
        case "hide":
            isShown = false
            window.orderOut(nil)
            writeResponse(snapshot(command: action))
        case "status":
            writeResponse(snapshot(command: action))
        case "quit":
            isShown = false
            window.orderOut(nil)
            writeResponse(snapshot(command: action))
            application.stop(nil)
        default:
            writeResponse(["event": "command", "command": action, "status": "FAIL",
                           "reason": "unsupported command"])
        }
    }

    @discardableResult
    private func placeOverTarget() -> Bool {
        guard let target = targetWindow(), let converted = appKitFrame(for: target.bounds) else { return false }
        window.setFrame(converted, display: false)
        return true
    }

    private func targetWindow() -> (id: Int, bounds: CGRect, row: [String: Any])? {
        guard let targetPID, let targetTitle,
              let rows = currentWindows() else { return nil }
        let matches = rows.compactMap { row -> (Int, CGRect, [String: Any])? in
            guard (row[kCGWindowOwnerPID as String] as? Int) == Int(targetPID),
                  (row[kCGWindowName as String] as? String) == targetTitle,
                  let id = row[kCGWindowNumber as String] as? Int,
                  let boundsValue = row[kCGWindowBounds as String] as? NSDictionary else { return nil }
            var bounds = CGRect.zero
            guard CGRectMakeWithDictionaryRepresentation(boundsValue as CFDictionary, &bounds),
                  bounds.width > 0, bounds.height > 0 else { return nil }
            return (id, bounds, row)
        }
        guard matches.count == 1, let item = matches.first else { return nil }
        return (item.0, item.1, item.2)
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

    private func currentWindows() -> [[String: Any]]? {
        CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]]
    }

    private func snapshot(command: String, failure: String? = nil) -> [String: Any] {
        let rows = currentWindows() ?? []
        let target = targetWindow()
        let covererID = window.windowNumber
        let covererIndex = rows.firstIndex { ($0[kCGWindowNumber as String] as? Int) == covererID }
        let targetIndex = target.flatMap { target in
            rows.firstIndex { ($0[kCGWindowNumber as String] as? Int) == target.id }
        }
        let covererRow = covererIndex.map { rows[$0] }
        let covererBounds = covererRow.flatMap { row -> CGRect? in
            guard let value = row[kCGWindowBounds as String] as? NSDictionary else { return nil }
            var rect = CGRect.zero
            return CGRectMakeWithDictionaryRepresentation(value as CFDictionary, &rect) ? rect : nil
        }
        var coverage = 0.0
        if let target, let covererBounds {
            let intersection = covererBounds.intersection(target.bounds)
            if !intersection.isNull && target.bounds.width > 0 && target.bounds.height > 0 {
                coverage = (intersection.width * intersection.height) / (target.bounds.width * target.bounds.height)
            }
        }
        var response: [String: Any] = [
            "event": "command",
            "command": command,
            "status": failure == nil ? "PASS" : "FAIL",
            "timestamp_uptime_ns": DispatchTime.now().uptimeNanoseconds,
            "coverer_pid": Int(getpid()),
            "coverer_window_number": covererID,
            "coverer_appkit_level": window.level.rawValue,
            "documented_modal_panel_level": NSWindow.Level.modalPanel.rawValue,
            "documented_floating_level": NSWindow.Level.floating.rawValue,
            "coverer_is_opaque": window.isOpaque,
            "coverer_alpha_value": window.alphaValue,
            "coverer_ignores_mouse_events": window.ignoresMouseEvents,
            "coverer_can_be_key": window.canBecomeKey,
            "coverer_can_be_main": window.canBecomeMain,
            "coverer_is_key": window.isKeyWindow,
            "coverer_is_main": window.isMainWindow,
            "coverer_is_visible": window.isVisible,
            "coverer_application_is_active": application.isActive,
            "target_pid": targetPID.map { Int($0) } as Any? ?? NSNull(),
            "target_title": targetTitle as Any? ?? NSNull(),
            "target_window_number": target?.id as Any? ?? NSNull(),
            "target_front_to_back_rank": targetIndex as Any? ?? NSNull(),
            "target_bounds_cg": target.map { rectJSON($0.bounds) } as Any? ?? NSNull(),
            "target_cg_layer": target?.row[kCGWindowLayer as String] as Any? ?? NSNull(),
            "target_alpha": target?.row[kCGWindowAlpha as String] as Any? ?? NSNull(),
            "coverer_front_to_back_rank": covererIndex as Any? ?? NSNull(),
            "coverer_bounds_cg": covererBounds.map(rectJSON) as Any? ?? NSNull(),
            "coverer_cg_layer": covererRow?[kCGWindowLayer as String] as Any? ?? NSNull(),
            "coverer_cg_alpha": covererRow?[kCGWindowAlpha as String] as Any? ?? NSNull(),
            "target_area_coverage_fraction": coverage,
            "front_of_target": (covererIndex != nil && targetIndex != nil && covererIndex! < targetIndex!),
            "target_fully_covered_by_geometry": coverage >= 0.999999,
            "state_mutated": command == "show" || command == "hide"
        ]
        if let failure { response["reason"] = failure }
        return response
    }

    private func rectJSON(_ rect: CGRect) -> [String: Double] {
        ["x": rect.origin.x, "y": rect.origin.y, "width": rect.width, "height": rect.height]
    }

    private func writeResponse(_ response: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

@main
private struct NeutralCovererMain {
    static func main() {
        CovererController().run()
    }
}
