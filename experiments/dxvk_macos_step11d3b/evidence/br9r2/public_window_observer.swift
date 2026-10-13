import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit
import Darwin
import Foundation

private let schemaVersion = 3
private let sampleIntervalSeconds = 0.5
private let telemetryMaximumAgeNS: UInt64 = 2_000_000_000
private let stableIntervalNS: UInt64 = 2_000_000_000
private let safetyWatchdogNS: UInt64 = 1_800_000_000_000
private let expectedCovererSHA256 = "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc"
private let visibleOcclusionBit = UInt64(NSWindow.OcclusionState.visible.rawValue)

struct Paths {
    let handoff: URL
    let completion: URL
    let result: URL
    let samples: URL
    let latest: URL
    let coverer: URL
    let expectedDisplayID: UInt32
    let runsRoot: URL
}

struct TargetHandoff {
    let raw: [String: Any]
    let runID: String
    let token: String
    let pid: pid_t
    let title: String
    let targetTracePath: URL
    let expectedTargetImagePath: URL
    let expectedTargetImageSHA256: String
    let expectedProcessPath: URL
    let expectedProcessSHA256: String
    let expectedProcessRole: String
    let runnerPID: Int
    let runnerMonotonicNS: UInt64
    let targetKind: String
    let telemetryExpected: Bool
}

func utcNow() -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: Date())
}

func boxed(_ value: Any?) -> Any { value ?? NSNull() }

func number(_ value: Any?) -> Double? {
    guard let number = value as? NSNumber else { return nil }
    return number.doubleValue
}

func integer(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber else { return nil }
    return number.intValue
}

func boolValue(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber else { return nil }
    return number.boolValue
}

func rect(_ value: Any?) -> CGRect? {
    guard let value = value as? [String: Any],
          let x = number(value["X"] ?? value["x"]),
          let y = number(value["Y"] ?? value["y"]),
          let width = number(value["Width"] ?? value["width"]),
          let height = number(value["Height"] ?? value["height"]),
          width > 0, height > 0 else { return nil }
    return CGRect(x: x, y: y, width: width, height: height)
}

func rectJSON(_ value: CGRect?) -> Any {
    guard let value else { return NSNull() }
    return [
        "x": value.origin.x, "y": value.origin.y,
        "width": value.width, "height": value.height,
        "minX": value.minX, "minY": value.minY,
        "maxX": value.maxX, "maxY": value.maxY,
    ]
}

func sha256(_ url: URL) -> String? {
    var info = stat()
    guard lstat(url.path, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG,
          let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }
    var digest = SHA256()
    do {
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            digest.update(data: chunk)
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    } catch {
        return nil
    }
}

func readJSONObject(_ url: URL) -> [String: Any]? {
    var info = stat()
    guard lstat(url.path, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG,
          let data = try? Data(contentsOf: url, options: [.mappedIfSafe]),
          let value = try? JSONSerialization.jsonObject(with: data),
          let object = value as? [String: Any] else { return nil }
    return object
}

func atomicJSON(_ object: [String: Any], to url: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
    let temporary = url.appendingPathExtension("tmp-\(getpid())-\(UUID().uuidString)")
    try data.write(to: temporary, options: [.atomic])
    guard rename(temporary.path, url.path) == 0 else {
        try? FileManager.default.removeItem(at: temporary)
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
}

func writeJSONFirstOrReplace(_ object: [String: Any], to url: URL) throws {
    if FileManager.default.fileExists(atPath: url.path) {
        try atomicJSON(object, to: url)
    } else {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: [.atomic])
    }
}

func appendJSON(_ object: [String: Any], to url: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    let line = data + Data([0x0a])
    let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    defer { close(fd) }
    var offset = 0
    try line.withUnsafeBytes { bytes in
        guard let baseAddress = bytes.baseAddress else { return }
        while offset < line.count {
            let written = Darwin.write(fd, baseAddress.advanced(by: offset), line.count - offset)
            guard written > 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
            offset += written
        }
    }
}

func windowRows(_ option: CGWindowListOption) -> [[String: Any]]? {
    CGWindowListCopyWindowInfo([option, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
}

func ownerPID(_ row: [String: Any]) -> pid_t? {
    (row[kCGWindowOwnerPID as String] as? NSNumber).map { pid_t($0.int32Value) }
}

struct ProcessExecutable {
    let pid: pid_t
    let path: String
}

func processListing(_ arguments: [String]) -> String? {
    let query = Process()
    query.executableURL = URL(fileURLWithPath: "/bin/ps")
    query.arguments = arguments
    let output = Pipe()
    query.standardOutput = output
    query.standardError = FileHandle.nullDevice
    do {
        try query.run()
    } catch {
        return nil
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    query.waitUntilExit()
    guard query.terminationStatus == 0,
          let text = String(data: data, encoding: .utf8) else { return nil }
    return text
}

func processPath(_ pid: pid_t) -> String? {
    guard pid > 0,
          let application = NSRunningApplication(processIdentifier: pid),
          let executableURL = application.executableURL else { return nil }
    let path = executableURL.standardizedFileURL.path
    guard path.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: path) else { return nil }
    return path
}

func processIsAlive(_ pid: pid_t) -> Bool {
    guard pid > 0 else { return false }
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

func targetProcessImageEvidence(_ handoff: TargetHandoff, telemetryTail: TelemetryTail,
                                processImageSHA256AtHandoff: String?)
    -> (path: String?, sha256: String?, matches: Bool, source: String) {
    let expectedPath = handoff.expectedProcessPath.standardizedFileURL.path
    if handoff.targetKind == "wine" {
        let processRow = telemetryTail.processIdentity
        let moduleRow = telemetryTail.wineMainExecutableIdentity
        let path = processRow?["main_executable_path"] as? String
        let digest = processRow?["main_executable_sha256"] as? String
        let moduleMatches = moduleRow?["identity_match"] as? Bool == true
        let matches = processIsAlive(handoff.pid)
            && telemetryTail.processIdentityMatchesPinnedImage
            && moduleMatches
            && path.map { URL(fileURLWithPath: $0).standardizedFileURL.path == expectedPath } == true
            && digest == handoff.expectedProcessSHA256
        return (path, digest, matches, "same-PID same-token process_identity and startup_module_identity trace")
    }
    let path = processPath(handoff.pid)
    let matches = processIsAlive(handoff.pid)
        && path.map { URL(fileURLWithPath: $0).standardizedFileURL.path == expectedPath } == true
        && processImageSHA256AtHandoff == handoff.expectedProcessSHA256
    return (path, processImageSHA256AtHandoff, matches,
            "NSRunningApplication executableURL and pinned file SHA-256")
}

func processExecutables() -> [ProcessExecutable]? {
    guard let text = processListing(["-ww", "-axo", "pid=,comm="]) else { return nil }
    var rows: [ProcessExecutable] = []
    for line in text.split(whereSeparator: \.isNewline) {
        let fields = line.split(maxSplits: 1, omittingEmptySubsequences: true,
                                whereSeparator: { $0.isWhitespace })
        guard fields.count == 2,
              let pidValue = Int32(fields[0]), pidValue > 0 else { return nil }
        let path = String(fields[1]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else { return nil }
        rows.append(ProcessExecutable(pid: pidValue, path: path))
    }
    return rows
}

func covererProcesses(_ covererURL: URL) -> [[String: Any]]? {
    let canonical = covererURL.standardizedFileURL.path
    var rows: [[String: Any]] = []
    guard let processes = processExecutables() else { return nil }
    for process in processes {
        let pid = process.pid
        let path = process.path
        let name = URL(fileURLWithPath: path).lastPathComponent
        let pathMatch = URL(fileURLWithPath: path).standardizedFileURL.path == canonical
        let nameMatch = name.localizedCaseInsensitiveContains("neutral-coverer")
        guard pathMatch || nameMatch else { continue }
        let digest = sha256(URL(fileURLWithPath: path))
        rows.append([
            "pid": Int(pid), "executablePath": path, "executableName": name,
            "pathMatchesPinnedCoverer": pathMatch,
            "sha256": boxed(digest),
            "sha256MatchesPinnedCoverer": digest == expectedCovererSHA256,
            "isAlive": kill(pid, 0) == 0,
        ])
    }
    return rows.sorted { (integer($0["pid"]) ?? 0) < (integer($1["pid"]) ?? 0) }
}

func screenRows() -> [[String: Any]] {
    NSScreen.screens.map { screen in
        let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
        let bounds = id.map { CGDisplayBounds($0) }
        return [
            "displayID": boxed(id.map { Int($0) }),
            "name": screen.localizedName,
            "isMainScreen": screen == NSScreen.main,
            "frameAppKit": rectJSON(screen.frame),
            "visibleFrameAppKit": rectJSON(screen.visibleFrame),
            "boundsCG": rectJSON(bounds),
            "backingScaleFactor": screen.backingScaleFactor,
        ]
    }
}

func displayRelationship(_ target: CGRect, expectedID: UInt32) -> [String: Any] {
    let matches = NSScreen.screens.compactMap { screen -> [String: Any]? in
        guard let idNumber = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        else { return nil }
        let id = idNumber.uint32Value
        let bounds = CGDisplayBounds(id)
        let center = bounds.contains(CGPoint(x: target.midX, y: target.midY))
        let fullyContains = bounds.contains(target)
        return [
            "displayID": Int(id),
            "name": screen.localizedName,
            "isMainScreen": screen == NSScreen.main,
            "centerContainsTarget": center,
            "fullyContainsTarget": fullyContains,
            "cgBounds": rectJSON(bounds),
        ]
    }
    let intendedBounds = CGDisplayBounds(expectedID)
    return [
        "expectedDisplayID": Int(expectedID),
        "expectedDisplayName": NSScreen.screens.first {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == expectedID
        }?.localizedName as Any? ?? NSNull(),
        "expectedDisplayCGBounds": rectJSON(intendedBounds),
        "targetCenterOnExpectedDisplay": intendedBounds.contains(CGPoint(x: target.midX, y: target.midY)),
        "targetFullyOnExpectedDisplay": intendedBounds.contains(target),
        "matchingScreens": matches,
        "uniqueExpectedDisplayMatch": matches.filter { integer($0["displayID"]) == Int(expectedID)
            && boolValue($0["fullyContainsTarget"]) == true }.count == 1,
    ]
}

func accessibilityAttributes(_ element: AXUIElement) -> [String: Any] {
    var result: [String: Any] = ["accessibilityTrusted": AXIsProcessTrusted()]
    var minimized: CFTypeRef?
    let minError = AXUIElementCopyAttributeValue(element, kAXMinimizedAttribute as CFString, &minimized)
    result["minimizedAttributeStatus"] = Int(minError.rawValue)
    result["minimized"] = minError == .success ? boxed((minimized as? NSNumber)?.boolValue) : NSNull()
    var focused: CFTypeRef?
    let focusedError = AXUIElementCopyAttributeValue(element, kAXFocusedAttribute as CFString, &focused)
    result["focusedAttributeStatus"] = Int(focusedError.rawValue)
    result["focused"] = focusedError == .success ? boxed((focused as? NSNumber)?.boolValue) : NSNull()
    var title: CFTypeRef?
    let titleError = AXUIElementCopyAttributeValue(element, kAXTitleAttribute as CFString, &title)
    result["title"] = titleError == .success ? boxed(title as? String) : NSNull()
    return result
}

func raiseTargetWindow(pid: pid_t, expectedTitle: String) -> [String: Any] {
    guard AXIsProcessTrusted() else {
        return ["attempted": false, "accessibilityTrusted": false, "reason": "AXIsProcessTrusted=false"]
    }
    let app = AXUIElementCreateApplication(pid)
    let frontmostError = AXUIElementSetAttributeValue(app, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    var windowsValue: CFTypeRef?
    let windowsError = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windowsValue)
    let windows = windowsValue as? [AXUIElement] ?? []
    var matches: [(AXUIElement, String?)] = []
    for window in windows {
        var titleValue: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &titleValue)
        let title = titleValue as? String
        if title == expectedTitle { matches.append((window, title)) }
    }
    let selected: AXUIElement? = matches.count == 1 ? matches[0].0 : (matches.isEmpty && windows.count == 1 ? windows[0] : nil)
    let raiseError = selected.map { AXUIElementPerformAction($0, kAXRaiseAction as CFString) }
    return [
        "attempted": true,
        "accessibilityTrusted": true,
        "frontmostAttributeStatus": Int(frontmostError.rawValue),
        "windowsAttributeStatus": Int(windowsError.rawValue),
        "windowCount": windows.count,
        "exactTitleMatchCount": matches.count,
        "raiseAttempted": selected != nil,
        "raiseActionStatus": raiseError.map { Int($0.rawValue) } as Any? ?? NSNull(),
        "requestedTitle": expectedTitle,
    ]
}

final class TelemetryTail {
    private var fd: Int32 = -1
    private var openedPath: String?
    private var offset: off_t = 0
    private var pending = Data()
    private(set) var latest: [String: Any]?
    private(set) var processIdentity: [String: Any]?
    private(set) var wineMainExecutableIdentity: [String: Any]?
    private(set) var identityError: String?
    private(set) var validRows = 0
    private(set) var rejectedRows = 0
    private let path: URL
    private let pid: pid_t
    private let token: String
    private let expectedProcessPath: String
    private let expectedProcessSHA256: String

    init(path: URL, pid: pid_t, token: String,
         expectedProcessPath: String, expectedProcessSHA256: String) {
        self.path = path
        self.pid = pid
        self.token = token
        self.expectedProcessPath = expectedProcessPath
        self.expectedProcessSHA256 = expectedProcessSHA256
    }

    deinit { if fd >= 0 { close(fd) } }

    func refresh() {
        if fd < 0 {
            var info = stat()
            guard lstat(path.path, &info) == 0,
                  (info.st_mode & S_IFMT) == S_IFREG else { return }
            fd = open(path.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
            guard fd >= 0 else { return }
            openedPath = path.path
        }
        guard openedPath == path.path else { return }
        var current = stat()
        guard fstat(fd, &current) == 0, (current.st_mode & S_IFMT) == S_IFREG else { return }
        let available = current.st_size - offset
        guard available > 0 else { return }
        var bytes = [UInt8](repeating: 0, count: min(Int(available), 2 * 1024 * 1024))
        let count = Darwin.read(fd, &bytes, bytes.count)
        guard count > 0 else { return }
        offset += off_t(count)
        pending.append(contentsOf: bytes.prefix(count))
        while let newline = pending.firstIndex(of: 0x0a) {
            let line = pending[..<newline]
            pending.removeSubrange(...newline)
            guard let value = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
            else { rejectedRows += 1; continue }
            guard value["trace_role"] as? String == "target",
                  integer(value["host_pid"]) == Int(pid),
                  integer(value["target_unix_pid"]) == Int(pid),
                  value["experiment_token"] as? String == token else { continue }
            if value["origin"] as? String == "INSTRUMENTATION",
               value["event"] as? String == "process_identity" {
                guard integer(value["pid"]) == Int(pid),
                      value["startup_id"] as? String == "pid-\(pid)",
                      let start = value["process_start_monotonic_ns"] as? NSNumber,
                      start.uint64Value > 0,
                      let executablePath = value["main_executable_path"] as? String,
                      let executableSHA256 = value["main_executable_sha256"] as? String else {
                    identityError = "same-PID Wine process_identity row is incomplete"
                    continue
                }
                guard URL(fileURLWithPath: executablePath).standardizedFileURL.path
                        == URL(fileURLWithPath: expectedProcessPath).standardizedFileURL.path,
                      executableSHA256 == expectedProcessSHA256 else {
                    identityError = "same-PID Wine process_identity row differs from the pinned host image"
                    continue
                }
                processIdentity = value
                continue
            }
            if value["origin"] as? String == "INSTRUMENTATION",
               value["event"] as? String == "startup_module_identity",
               value["role"] as? String == "wine" {
                guard value["startup_id"] as? String == "pid-\(pid)",
                      value["identity_kind"] as? String == "dyld-main-executable",
                      value["loaded_in_process"] as? Bool == true,
                      value["identity_match"] as? Bool == true,
                      value["path"] as? String == expectedProcessPath,
                      value["sha256"] as? String == expectedProcessSHA256,
                      value["observed_path"] as? String == expectedProcessPath,
                      value["observed_sha256"] as? String == expectedProcessSHA256 else {
                    identityError = "same-PID Wine main-executable module identity differs from the pinned host image"
                    continue
                }
                wineMainExecutableIdentity = value
                continue
            }
            guard value["event"] as? String == "window_telemetry",
                  value["origin"] as? String == "LAYER_SOURCE",
                  let timestamp = value["ts_mach_ns"] as? NSNumber,
                  timestamp.uint64Value > 0,
                  integer(value["telemetry_sequence"]) != nil else { continue }
            latest = value
            validRows += 1
        }
    }

    var processIdentityMatchesPinnedImage: Bool {
        processIdentity != nil && wineMainExecutableIdentity != nil && identityError == nil
    }
}

func isCanonicalDirectory(_ url: URL) -> Bool {
    var info = stat()
    guard lstat(url.path, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFDIR,
          url.standardizedFileURL.path == url.path,
          url.resolvingSymlinksInPath().path == url.path else { return false }
    return true
}

func canonicalTracePath(_ rawPath: String, runID: String, pid: pid_t,
                        targetKind: String, runsRoot: URL) -> URL? {
    guard runID.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                      options: .regularExpression) != nil,
          isCanonicalDirectory(runsRoot) else { return nil }
    let runDirectory = runsRoot.appendingPathComponent(runID, isDirectory: true)
    guard isCanonicalDirectory(runDirectory) else { return nil }
    let relativeTrace: String
    if targetKind == "wine" {
        relativeTrace = "host-traces/host-trace.\(pid).jsonl"
    } else if targetKind == "native" {
        relativeTrace = "trace.jsonl"
    } else {
        return nil
    }
    let expected = runDirectory.appendingPathComponent(relativeTrace).standardizedFileURL
    let supplied = URL(fileURLWithPath: rawPath).standardizedFileURL
    guard supplied.path == rawPath, supplied.path == expected.path else { return nil }
    if targetKind == "wine" {
        let hostTraceDirectory = runDirectory.appendingPathComponent("host-traces", isDirectory: true)
        guard isCanonicalDirectory(hostTraceDirectory) else { return nil }
    }
    return expected
}

func parseHandoff(_ raw: [String: Any], runsRoot: URL) -> TargetHandoff? {
    guard raw["schema_version"] as? Int == 2,
          let runID = raw["run_id"] as? String, !runID.isEmpty,
          let token = raw["experiment_token"] as? String,
          token.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil,
          let pidNumber = raw["target_unix_pid"] as? NSNumber, pidNumber.int32Value > 0,
          let title = raw["target_title"] as? String, !title.isEmpty,
          let trace = raw["target_trace_path"] as? String, trace.hasPrefix("/"),
          let runnerPID = raw["runner_pid"] as? Int, runnerPID > 0,
          let monotonic = raw["runner_monotonic_ns"] as? NSNumber,
          monotonic.uint64Value > 0,
          let targetKind = raw["target_kind"] as? String,
          ["wine", "native"].contains(targetKind),
          let telemetryExpected = raw["window_telemetry_expected"] as? Bool else { return nil }
    guard let canonicalTrace = canonicalTracePath(
        trace, runID: runID, pid: pid_t(pidNumber.int32Value),
        targetKind: targetKind, runsRoot: runsRoot) else { return nil }
    let imageIdentityKey = targetKind == "wine" ? "expected_pe_identity" : "expected_native_identity"
    guard let imageIdentity = raw[imageIdentityKey] as? [String: Any],
          let imagePath = imageIdentity["path"] as? String, imagePath.hasPrefix("/"),
          let imageHash = imageIdentity["sha256"] as? String,
          imageHash.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
          (targetKind == "wine" ? telemetryExpected : !telemetryExpected) else { return nil }
    guard let processIdentity = raw["expected_target_process_identity"] as? [String: Any],
          let processPath = processIdentity["path"] as? String, processPath.hasPrefix("/"),
          let processHash = processIdentity["sha256"] as? String,
          processHash.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
        return nil
    }
    let processRole = processIdentity["role"] as? String ?? "target_process"
    guard !processRole.isEmpty,
          (targetKind != "wine" || processRole == "wine_host_image") else { return nil }
    return TargetHandoff(
        raw: raw, runID: runID, token: token, pid: pid_t(pidNumber.int32Value),
        title: title, targetTracePath: canonicalTrace,
        expectedTargetImagePath: URL(fileURLWithPath: imagePath), expectedTargetImageSHA256: imageHash,
        expectedProcessPath: URL(fileURLWithPath: processPath),
        expectedProcessSHA256: processHash, expectedProcessRole: processRole,
        runnerPID: runnerPID, runnerMonotonicNS: monotonic.uint64Value,
        targetKind: targetKind, telemetryExpected: telemetryExpected
    )
}

func targetWindowRows(_ rows: [[String: Any]], pid: pid_t, title: String) -> (row: [String: Any]?, error: String?, ownerCount: Int) {
    let owned = rows.filter { ownerPID($0) == pid }
    if owned.isEmpty { return (nil, nil, 0) }
    let titleMatches = owned.filter { ($0[kCGWindowName as String] as? String) == title }
    if titleMatches.count == 1 { return (titleMatches[0], nil, owned.count) }
    if titleMatches.count > 1 { return (nil, "multiple owner-PID windows have the expected title", owned.count) }
    if owned.count == 1 { return (owned[0], nil, owned.count) }
    let normal = owned.filter {
        integer($0[kCGWindowLayer as String]) == 0
            && (number($0[kCGWindowAlpha as String]) ?? 0) > 0.95
            && rect($0[kCGWindowBounds as String]) != nil
    }
    if normal.count == 1 { return (normal[0], nil, owned.count) }
    return (nil, "target PID owns multiple windows without a unique expected-title/window match", owned.count)
}

func windowEvidence(_ row: [String: Any], rank: Int?, target: CGRect) -> [String: Any] {
    let bounds = rect(row[kCGWindowBounds as String])
    let overlap = bounds.map { $0.intersection(target) } ?? .null
    let intersects = !overlap.isNull && overlap.width > 0 && overlap.height > 0
    let full = bounds.map {
        $0.minX <= target.minX && $0.minY <= target.minY
            && $0.maxX >= target.maxX && $0.maxY >= target.maxY
    } ?? false
    let alpha = number(row[kCGWindowAlpha as String])
    return [
        "rankFrontToBack": boxed(rank),
        "windowID": boxed(integer(row[kCGWindowNumber as String])),
        "ownerPID": boxed(ownerPID(row).map { Int($0) }),
        "ownerName": boxed(row[kCGWindowOwnerName as String] as? String),
        "title": boxed(row[kCGWindowName as String] as? String),
        "layer": boxed(integer(row[kCGWindowLayer as String])),
        "alpha": boxed(alpha),
        "onScreen": boolValue(row[kCGWindowIsOnscreen as String]) ?? (rank != nil),
        "bounds": rectJSON(bounds),
        "intersectsTarget": intersects,
        "intersectionFraction": intersects ? (overlap.width * overlap.height) / (target.width * target.height) : 0,
        "fullyContainsTarget": full,
        "opaqueFullCoverCandidate": full && (alpha ?? 0) >= 0.99,
    ]
}

func selectedAXWindow(pid: pid_t, title: String) -> AXUIElement? {
    let app = AXUIElementCreateApplication(pid)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
          let windows = value as? [AXUIElement] else { return nil }
    let matching = windows.filter { window in
        var titleValue: CFTypeRef?
        return AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &titleValue) == .success
            && (titleValue as? String) == title
    }
    if matching.count == 1 { return matching[0] }
    return matching.isEmpty && windows.count == 1 ? windows[0] : nil
}

func telemetryEvidence(_ telemetry: [String: Any]?, now: UInt64) -> [String: Any] {
    guard let telemetry,
          let stamp = (telemetry["ts_mach_ns"] as? NSNumber)?.uint64Value else {
        return ["available": false, "fresh": false, "reason": "no target-owned AppKit telemetry record"]
    }
    let age = now >= stamp ? now - stamp : UInt64.max
    let state = (telemetry["window_occlusion_state"] as? NSNumber)?.uint64Value
    let visibleBit = state.map { ($0 & visibleOcclusionBit) != 0 }
    return [
        "available": true,
        "fresh": age <= telemetryMaximumAgeNS,
        "ageNS": age == UInt64.max ? NSNull() : age as Any,
        "source": "target-owned LAYER_SOURCE window_telemetry from the run trace",
        "timestampMachNS": stamp,
        "telemetrySequence": boxed(telemetry["telemetry_sequence"]),
        "windowID": boxed(telemetry["window_id"]),
        "windowAssociated": boxed(telemetry["window_associated"]),
        "windowVisible": boxed(telemetry["window_visible"]),
        "windowKey": boxed(telemetry["window_key"]),
        "windowMain": boxed(telemetry["window_main"]),
        "windowOccluded": boxed(telemetry["window_occluded"]),
        "windowOcclusionState": boxed(state),
        "visibleBitSet": boxed(visibleBit),
        "windowMiniaturized": boxed(telemetry["window_miniaturized"]),
        "appActive": boxed(telemetry["app_active"]),
        "windowAlpha": boxed(telemetry["window_alpha"]),
        "windowFrame": [
            "x": boxed(telemetry["window_frame_x"]),
            "y": boxed(telemetry["window_frame_y"]),
            "width": boxed(telemetry["window_frame_width"]),
            "height": boxed(telemetry["window_frame_height"]),
        ],
    ]
}

func isCanonicalUncovered(_ sample: [String: Any]) -> Bool {
    guard let target = sample["target"] as? [String: Any],
          let targetWindowID = integer(target["windowID"]), targetWindowID > 0,
          let targetRank = integer(target["rankFrontToBack"]), targetRank >= 0,
          target["rankSource"] as? String == "onScreenWindowList",
          let accessibility = target["accessibility"] as? [String: Any],
          let app = sample["app"] as? [String: Any],
          let targetKind = sample["targetKind"] as? String,
          let telemetryExpected = sample["windowTelemetryExpected"] as? Bool else { return false }
    let display = target["displayRelationship"] as? [String: Any]
    let externallyEligible = target["onScreen"] as? Bool == true
        && target["titleMatched"] as? Bool == true
        && target["processAlive"] as? Bool == true
        && target["windowIdentityAmbiguous"] as? Bool == false
        && target["processIdentityMatches"] as? Bool == true
        && target["accessibilityMinimized"] as? Bool == false
        && accessibility["accessibilityTrusted"] as? Bool == true
        && accessibility["focused"] as? Bool == true
        && (display?["uniqueExpectedDisplayMatch"] as? Bool == true)
        && (display?["targetFullyOnExpectedDisplay"] as? Bool == true)
        && (sample["allWindowListQuerySucceeded"] as? Bool == true)
        && (sample["onScreenWindowListQuerySucceeded"] as? Bool == true)
        && sample["aboveTargetWindowDetailsComplete"] as? Bool == true
        && (sample["covererProcessEnumerationSucceeded"] as? Bool == true)
        && sample["alphaOpaqueFullCoverCandidatePresent"] as? Bool == false
        && sample["neutralCovererProcessPresent"] as? Bool == false
        && sample["neutralCovererWindowPresent"] as? Bool == false
        && app["isActive"] as? Bool == true
    guard externallyEligible else { return false }
    if targetKind == "native" {
        return !telemetryExpected
    }
    guard telemetryExpected,
          let telemetry = sample["inProcessTelemetry"] as? [String: Any] else { return false }
    return telemetry["available"] as? Bool == true
        && telemetry["fresh"] as? Bool == true
        && telemetry["windowVisible"] as? Bool == true
        && telemetry["windowOccluded"] as? Bool == false
        && telemetry["visibleBitSet"] as? Bool == true
        && telemetry["windowMiniaturized"] as? Bool == false
        && telemetry["windowKey"] as? Bool == true
        && telemetry["windowMain"] as? Bool == true
        && telemetry["appActive"] as? Bool == true
}

func isCovered(_ sample: [String: Any], expectedCovererPID: Int? = nil,
               expectedCovererWindowID: Int? = nil) -> Bool {
    guard let target = sample["target"] as? [String: Any],
          let coverers = sample["neutralCovererProcesses"] as? [[String: Any]],
          let windows = sample["neutralCovererWindows"] as? [[String: Any]],
          let targetID = integer(target["windowID"]), targetID > 0,
          let targetRank = integer(target["rankFrontToBack"]), targetRank >= 0 else { return false }
    if sample["covererProcessEnumerationSucceeded"] as? Bool != true
        || sample["allWindowListQuerySucceeded"] as? Bool != true
        || sample["onScreenWindowListQuerySucceeded"] as? Bool != true
        || target["rankSource"] as? String != "onScreenWindowList"
        || target["titleMatched"] as? Bool != true
        || target["processAlive"] as? Bool != true
        || target["processIdentityMatches"] as? Bool != true { return false }
    for process in coverers {
        guard process["sha256MatchesPinnedCoverer"] as? Bool == true,
              let pid = integer(process["pid"]),
              expectedCovererPID == nil || expectedCovererPID == pid else { continue }
        for window in windows {
            guard integer(window["ownerPID"]) == pid,
                  let covererWindowID = integer(window["windowID"]), covererWindowID > 0,
                  expectedCovererWindowID == nil || covererWindowID == expectedCovererWindowID,
                  window["onScreen"] as? Bool == true,
                  window["fullyContainsTarget"] as? Bool == true,
                  window["rankSource"] as? String == "onScreenWindowList",
                  (number(window["alpha"]) ?? 0) >= 0.99,
                  let rank = integer(window["rankFrontToBack"]),
                  rank < targetRank,
                  integer(target["windowID"]) == targetID,
                  let sameDisplay = target["covererOnExpectedDisplay"] as? Bool,
                  sameDisplay else { continue }
            if sample["windowTelemetryExpected"] as? Bool == true {
                let telemetry = sample["inProcessTelemetry"] as? [String: Any]
                guard telemetry?["available"] as? Bool == true,
                      telemetry?["fresh"] as? Bool == true,
                      telemetry?["windowOccluded"] as? Bool == true,
                      telemetry?["visibleBitSet"] as? Bool == false else { continue }
            }
            return true
        }
    }
    return false
}

func isRestored(_ sample: [String: Any]) -> Bool {
    guard let target = sample["target"] as? [String: Any],
          let targetWindowID = integer(target["windowID"]), targetWindowID > 0,
          let targetRank = integer(target["rankFrontToBack"]), targetRank >= 0,
          target["rankSource"] as? String == "onScreenWindowList",
          let accessibility = target["accessibility"] as? [String: Any],
          let app = sample["app"] as? [String: Any],
          let targetKind = sample["targetKind"] as? String,
          let telemetryExpected = sample["windowTelemetryExpected"] as? Bool,
          sample["covererProcessEnumerationSucceeded"] as? Bool == true,
          sample["allWindowListQuerySucceeded"] as? Bool == true,
          sample["onScreenWindowListQuerySucceeded"] as? Bool == true,
          let covererWindows = sample["neutralCovererWindows"] as? [[String: Any]] else { return false }
    let covererDoesNotObscureTarget = covererWindows.allSatisfy { window in
        guard let onScreen = window["onScreen"] as? Bool else { return false }
        if !onScreen { return true }
        guard window["boundsValid"] as? Bool == true,
              let intersects = window["intersectsTarget"] as? Bool else { return false }
        if !intersects { return true }
        guard let covererWindowID = integer(window["windowID"]), covererWindowID > 0,
              let alpha = number(window["alpha"]),
              let rank = integer(window["rankFrontToBack"]),
              window["rankSource"] as? String == "onScreenWindowList",
              targetRank >= 0 else { return false }
        if alpha < 0.99 || rank >= targetRank { return true }
        guard let fullyContains = window["fullyContainsTarget"] as? Bool else { return false }
        return !fullyContains
    }
    let display = target["displayRelationship"] as? [String: Any]
    let uniqueExpectedDisplay = display?["uniqueExpectedDisplayMatch"] as? Bool == true
    guard target["onScreen"] as? Bool == true
        && target["titleMatched"] as? Bool == true
        && target["processAlive"] as? Bool == true
        && target["windowIdentityAmbiguous"] as? Bool == false
        && target["processIdentityMatches"] as? Bool == true
        && target["accessibilityMinimized"] as? Bool == false
        && accessibility["accessibilityTrusted"] as? Bool == true
        && accessibility["focused"] as? Bool == true
        && uniqueExpectedDisplay
        && sample["aboveTargetWindowDetailsComplete"] as? Bool == true
        && sample["alphaOpaqueFullCoverCandidatePresent"] as? Bool == false
        && sample["allWindowListQuerySucceeded"] as? Bool == true
        && sample["onScreenWindowListQuerySucceeded"] as? Bool == true
        && sample["covererProcessEnumerationSucceeded"] as? Bool == true
        && app["isActive"] as? Bool == true
        && covererDoesNotObscureTarget else { return false }
    if targetKind == "native" { return !telemetryExpected }
    guard telemetryExpected,
          let telemetry = sample["inProcessTelemetry"] as? [String: Any] else { return false }
    return telemetry["available"] as? Bool == true
        && telemetry["fresh"] as? Bool == true
        && telemetry["windowVisible"] as? Bool == true
        && telemetry["windowOccluded"] as? Bool == false
        && telemetry["visibleBitSet"] as? Bool == true
        && telemetry["windowMiniaturized"] as? Bool == false
        && telemetry["windowKey"] as? Bool == true
        && telemetry["windowMain"] as? Bool == true
        && telemetry["appActive"] as? Bool == true
}

func streakSummary(_ samples: [[String: Any]], predicate: ([String: Any]) -> Bool) -> [String: Any] {
    var bestStart: UInt64?
    var bestEnd: UInt64?
    var currentStart: UInt64?
    var currentEnd: UInt64?
    var currentCount = 0
    var currentWindowID: Int?
    var currentTelemetryRequired = false
    var firstTelemetrySequence: Int?
    var lastTelemetrySequence: Int?
    var firstTelemetryTimestamp: UInt64?
    var lastTelemetryTimestamp: UInt64?
    var bestCount = 0
    var bestTelemetryRequired = false
    var bestTelemetryProgressVerified = false
    var bestTelemetrySequenceAdvanceCount = 0
    for sample in samples {
        guard let stamp = sample["sampledAtMonotonicNS"] as? UInt64,
              predicate(sample) else {
            currentStart = nil; currentEnd = nil; currentCount = 0; currentWindowID = nil
            currentTelemetryRequired = false
            firstTelemetrySequence = nil; lastTelemetrySequence = nil
            firstTelemetryTimestamp = nil; lastTelemetryTimestamp = nil
            continue
        }
        let windowID = integer((sample["target"] as? [String: Any])?["windowID"])
        if let previous = currentEnd,
           stamp <= previous || stamp - previous > 1_500_000_000 || windowID != currentWindowID {
            currentStart = nil; currentEnd = nil; currentCount = 0
            currentTelemetryRequired = false
            firstTelemetrySequence = nil; lastTelemetrySequence = nil
            firstTelemetryTimestamp = nil; lastTelemetryTimestamp = nil
        }
        let telemetryRequired = sample["windowTelemetryExpected"] as? Bool == true
        if telemetryRequired {
            let telemetry = sample["inProcessTelemetry"] as? [String: Any]
            guard let sequence = integer(telemetry?["telemetrySequence"]), sequence >= 0,
                  let telemetryTimestamp = (telemetry?["timestampMachNS"] as? NSNumber)?.uint64Value,
                  telemetryTimestamp > 0, telemetryTimestamp <= stamp,
                  stamp - telemetryTimestamp <= telemetryMaximumAgeNS,
                  telemetry?["available"] as? Bool == true,
                  telemetry?["fresh"] as? Bool == true else {
                currentStart = nil; currentEnd = nil; currentCount = 0; currentWindowID = nil
                currentTelemetryRequired = false
                firstTelemetrySequence = nil; lastTelemetrySequence = nil
                firstTelemetryTimestamp = nil; lastTelemetryTimestamp = nil
                continue
            }
            if let previousSequence = lastTelemetrySequence,
               let previousTimestamp = lastTelemetryTimestamp {
                if sequence < previousSequence || telemetryTimestamp < previousTimestamp
                    || (sequence == previousSequence && telemetryTimestamp != previousTimestamp)
                    || (sequence > previousSequence && telemetryTimestamp <= previousTimestamp) {
                    currentStart = nil; currentEnd = nil; currentCount = 0; currentWindowID = nil
                    currentTelemetryRequired = false
                    firstTelemetrySequence = nil; lastTelemetrySequence = nil
                    firstTelemetryTimestamp = nil; lastTelemetryTimestamp = nil
                    continue
                }
            } else {
                currentTelemetryRequired = true
                firstTelemetrySequence = sequence
                firstTelemetryTimestamp = telemetryTimestamp
            }
            lastTelemetrySequence = sequence
            lastTelemetryTimestamp = telemetryTimestamp
        }
        if currentStart == nil { currentStart = stamp; currentWindowID = windowID }
        currentEnd = stamp
        currentCount += 1
        let telemetryProgressVerified = !currentTelemetryRequired
            || ((lastTelemetrySequence ?? 0) > (firstTelemetrySequence ?? 0)
                && (lastTelemetryTimestamp ?? 0) > (firstTelemetryTimestamp ?? 0))
        let stable = currentStart.map { stamp >= $0 + stableIntervalNS } == true && currentCount >= 3
            && telemetryProgressVerified
        if stable, currentCount > bestCount {
            bestStart = currentStart; bestEnd = stamp; bestCount = currentCount
            bestTelemetryRequired = currentTelemetryRequired
            bestTelemetryProgressVerified = telemetryProgressVerified
            bestTelemetrySequenceAdvanceCount = ((lastTelemetrySequence ?? 0) - (firstTelemetrySequence ?? 0))
        }
    }
    let attained = bestStart != nil && bestEnd != nil
    return [
        "attained": attained,
        "firstSampleMonotonicNS": boxed(bestStart),
        "stabilityProvenMonotonicNS": boxed(bestEnd),
        "stableDurationNS": bestStart.flatMap { start in bestEnd.map { $0 - start } } as Any? ?? NSNull(),
        "sampleCountInLongestQualifyingStreak": bestCount,
        "telemetryProgressRequired": bestTelemetryRequired,
        "telemetryProgressVerified": bestTelemetryProgressVerified,
        "telemetrySequenceAdvanceCountInLongestQualifyingStreak": bestTelemetrySequenceAdvanceCount,
        "requiredSampleCount": 3,
        "requiredStableIntervalNS": stableIntervalNS,
    ]
}

func makeSample(_ handoff: TargetHandoff, paths: Paths, allRows: [[String: Any]],
                visibleRows: [[String: Any]], allWindowQuerySucceeded: Bool,
                onScreenWindowQuerySucceeded: Bool,
                queryStartedMonotonicNS: UInt64, queryCompletedMonotonicNS: UInt64,
                telemetryTail: TelemetryTail,
                processImageSHA256AtHandoff: String?,
                activation: [String: Any], identity: [String: Any],
                previousWindowID: Int?) -> ([String: Any], Int?, String?) {
    let now = DispatchTime.now().uptimeNanoseconds
    let processQueryStarted = DispatchTime.now().uptimeNanoseconds
    let processRowsResult = covererProcesses(paths.coverer)
    let processQueryCompleted = DispatchTime.now().uptimeNanoseconds
    let processRows = processRowsResult ?? []
    let covererPIDs = Set(processRows.compactMap { integer($0["pid"]) })
    let rawCovererWindows = allRows.enumerated().compactMap { allRank, row -> [String: Any]? in
        let owner = ownerPID(row)
        let ownerName = (row[kCGWindowOwnerName as String] as? String ?? "")
        let title = (row[kCGWindowName as String] as? String ?? "")
        guard owner.map(Int.init).map(covererPIDs.contains) == true
                || ownerName.localizedCaseInsensitiveContains("neutral-coverer")
                || title.localizedCaseInsensitiveContains("neutral-coverer") else { return nil }
        let visibleRank = integer(row[kCGWindowNumber as String]).flatMap { windowID in
            visibleRows.firstIndex { integer($0[kCGWindowNumber as String]) == windowID }
        }
        return [
            "windowID": boxed(integer(row[kCGWindowNumber as String])),
            "ownerPID": boxed(owner.map { Int($0) }),
            "ownerName": ownerName, "title": title,
            "layer": boxed(integer(row[kCGWindowLayer as String])),
            "alpha": boxed(number(row[kCGWindowAlpha as String])),
            "onScreen": boolValue(row[kCGWindowIsOnscreen as String]) ?? (visibleRank != nil),
            "rankFrontToBack": boxed(visibleRank),
            "rankSource": "onScreenWindowList",
            "rankAllWindowList": allRank,
            "boundsValid": rect(row[kCGWindowBounds as String]) != nil,
            "bounds": rectJSON(rect(row[kCGWindowBounds as String])),
        ]
    }
    let owner = targetWindowRows(allRows, pid: handoff.pid, title: handoff.title)
    let imageEvidence = targetProcessImageEvidence(
        handoff, telemetryTail: telemetryTail,
        processImageSHA256AtHandoff: processImageSHA256AtHandoff)
    let app = NSRunningApplication(processIdentifier: handoff.pid)
    let appActive: Bool? = handoff.targetKind == "wine"
        ? boolValue(telemetryTail.latest?["app_active"]) : app?.isActive
    guard let targetRow = owner.row,
          let targetBounds = rect(targetRow[kCGWindowBounds as String]) else {
        let sample: [String: Any] = [
            "event": "sample", "sampledAtUTC": utcNow(), "sampledAtMonotonicNS": now,
            "query": [
                "status": owner.error == nil ? "TARGET_WINDOW_NOT_DISCOVERED" : "AMBIGUOUS",
                "allWindowCount": allRows.count, "onScreenWindowCount": visibleRows.count,
                "allWindowListQuerySucceeded": allWindowQuerySucceeded,
                "onScreenWindowListQuerySucceeded": onScreenWindowQuerySucceeded,
                "queryStartedMonotonicNS": queryStartedMonotonicNS,
                "queryCompletedMonotonicNS": queryCompletedMonotonicNS,
                "targetOwnerWindowCount": owner.ownerCount, "targetWindowAmbiguity": boxed(owner.error),
                "targetOwnerPID": Int(handoff.pid),
            ],
            "targetKind": handoff.targetKind,
            "windowTelemetryExpected": handoff.telemetryExpected,
            "allWindowListQuerySucceeded": allWindowQuerySucceeded,
            "onScreenWindowListQuerySucceeded": onScreenWindowQuerySucceeded,
            "covererProcessEnumerationSucceeded": processRowsResult != nil,
            "covererProcessQueryStartedMonotonicNS": processQueryStarted,
            "covererProcessQueryCompletedMonotonicNS": processQueryCompleted,
            "target": ["ownerPID": Int(handoff.pid), "windowID": NSNull(),
                       "onScreen": false, "windowIdentityAmbiguous": owner.error != nil,
                       "processAlive": processIsAlive(handoff.pid),
                       "processImagePath": boxed(imageEvidence.path),
                       "processImageSHA256": boxed(imageEvidence.sha256),
                       "processImageVerificationSource": imageEvidence.source,
                       "processIdentityMatches": imageEvidence.matches],
            "app": ["isActive": boxed(appActive),
                    "isActiveSource": handoff.targetKind == "wine"
                        ? "same-PID in-process AppKit telemetry" : "NSRunningApplication",
                    "runningApplicationAvailable": app != nil],
            "inProcessTelemetry": telemetryEvidence(telemetryTail.latest, now: now),
            "inProcessProcessIdentity": [
                "verified": telemetryTail.processIdentityMatchesPinnedImage,
                "error": boxed(telemetryTail.identityError),
                "processIdentity": boxed(telemetryTail.processIdentity),
                "mainExecutableModuleIdentity": boxed(telemetryTail.wineMainExecutableIdentity),
            ],
            "neutralCovererProcesses": processRows,
            "neutralCovererWindows": rawCovererWindows,
            "neutralCovererProcessPresent": processRowsResult == nil || !processRows.isEmpty,
            "neutralCovererWindowPresent": !rawCovererWindows.isEmpty,
            "neutralCovererCoveringWindowPresent": false,
            "alphaOpaqueFullCoverCandidatePresent": false,
            "activation": activation,
            "queryAttempt": true,
        ]
        var completedSample = sample
        completedSample["canonicalUncoveredEligible"] = isCanonicalUncovered(completedSample)
        completedSample["coveredEligible"] = isCovered(completedSample)
        completedSample["restoredEligible"] = isRestored(completedSample)
        return (completedSample, previousWindowID, owner.error)
    }

    let windowID = integer(targetRow[kCGWindowNumber as String])
    let onScreenRank = windowID.flatMap { id in
        visibleRows.firstIndex { integer($0[kCGWindowNumber as String]) == id }
    }
    let onScreen = onScreenRank != nil
        && (boolValue(targetRow[kCGWindowIsOnscreen as String]) ?? true)
    let rankRows: [[String: Any]] = onScreen ? visibleRows : []
    let rank = onScreenRank
    let aboveRows = Array(rankRows.prefix(rank ?? 0))
    let aboveDetailsComplete = aboveRows.allSatisfy { row in
        rect(row[kCGWindowBounds as String]) != nil
            && number(row[kCGWindowAlpha as String]) != nil
            && integer(row[kCGWindowNumber as String]) != nil
    }
    let above = aboveRows.enumerated().compactMap { index, row -> [String: Any]? in
        guard let bounds = rect(row[kCGWindowBounds as String]) else { return nil }
        let intersection = bounds.intersection(targetBounds)
        guard !intersection.isNull, intersection.width > 0, intersection.height > 0 else { return nil }
        return windowEvidence(row, rank: index, target: targetBounds)
    }
    let opaqueCover = above.contains { $0["opaqueFullCoverCandidate"] as? Bool == true }
    let covererWindows = rawCovererWindows.map { row -> [String: Any] in
        var evidence = row
        let bounds = rect(row["bounds"])
        let intersection = bounds.map { $0.intersection(targetBounds) } ?? .null
        let intersects = !intersection.isNull && intersection.width > 0 && intersection.height > 0
        let fullyContains = bounds.map {
            $0.minX <= targetBounds.minX && $0.minY <= targetBounds.minY
                && $0.maxX >= targetBounds.maxX && $0.maxY >= targetBounds.maxY
        } ?? false
        evidence["intersectsTarget"] = intersects
        evidence["boundsValid"] = bounds != nil
        evidence["intersectionFraction"] = intersects
            ? (intersection.width * intersection.height) / (targetBounds.width * targetBounds.height) : 0
        evidence["fullyContainsTarget"] = fullyContains
        evidence["opaqueFullCoverCandidate"] = fullyContains && (number(row["alpha"]) ?? 0) >= 0.99
        return evidence
    }
    let telemetry = telemetryEvidence(telemetryTail.latest, now: now)
    let telemetryRow = telemetryTail.latest
    let expectedProcessPath = handoff.expectedProcessPath.standardizedFileURL.path
    let ax = selectedAXWindow(pid: handoff.pid, title: handoff.title).map(accessibilityAttributes) ?? [:]
    let appKitMini = boolValue(telemetryRow?["window_miniaturized"])
    let targetDisplay = displayRelationship(targetBounds, expectedID: paths.expectedDisplayID)
    let covererOnExpectedDisplay = covererWindows.contains { window in
        guard let boundsValue = window["bounds"] as? [String: Any],
              let bounds = rect(boundsValue) else { return false }
        let display = displayRelationship(bounds, expectedID: paths.expectedDisplayID)
        return boolValue(display["targetFullyOnExpectedDisplay"]) == true
            && integer(window["rankFrontToBack"]).map { $0 < (rank ?? Int.max) } == true
    }
    let identityChanged = previousWindowID != nil && windowID != previousWindowID
    let sample: [String: Any] = [
        "event": "sample",
        "targetKind": handoff.targetKind,
        "windowTelemetryExpected": handoff.telemetryExpected,
        "allWindowListQuerySucceeded": allWindowQuerySucceeded,
        "onScreenWindowListQuerySucceeded": onScreenWindowQuerySucceeded,
        "covererProcessEnumerationSucceeded": processRowsResult != nil,
        "aboveTargetWindowDetailsComplete": aboveDetailsComplete,
        "covererProcessQueryStartedMonotonicNS": processQueryStarted,
        "covererProcessQueryCompletedMonotonicNS": processQueryCompleted,
        "sampledAtUTC": utcNow(),
        "sampledAtMonotonicNS": now,
        "query": [
            "status": identityChanged ? "TARGET_WINDOW_ID_CHANGED" : "TARGET_WINDOW_FOUND",
            "allWindowCount": allRows.count, "onScreenWindowCount": visibleRows.count,
            "allWindowListQuerySucceeded": allWindowQuerySucceeded,
            "onScreenWindowListQuerySucceeded": onScreenWindowQuerySucceeded,
            "queryStartedMonotonicNS": queryStartedMonotonicNS,
            "queryCompletedMonotonicNS": queryCompletedMonotonicNS,
            "targetOwnerWindowCount": owner.ownerCount,
            "expectedTitleMatch": (targetRow[kCGWindowName as String] as? String) == handoff.title,
            "windowIDChanged": identityChanged,
        ],
        "target": [
            "ownerPID": Int(handoff.pid), "ownerName": boxed(targetRow[kCGWindowOwnerName as String]),
            "title": boxed(targetRow[kCGWindowName as String]),
            "expectedTitle": handoff.title, "titleMatched": (targetRow[kCGWindowName as String] as? String) == handoff.title,
            "windowID": boxed(windowID), "rankFrontToBack": boxed(rank),
            "rankSource": "onScreenWindowList",
            "layer": boxed(integer(targetRow[kCGWindowLayer as String])),
            "alpha": boxed(number(targetRow[kCGWindowAlpha as String])),
            "bounds": rectJSON(targetBounds), "onScreen": onScreen,
            "processAlive": processIsAlive(handoff.pid),
            "cgWindowIsOnscreen": boxed(targetRow[kCGWindowIsOnscreen as String]),
            "appKitMiniaturized": boxed(appKitMini),
            "accessibilityMinimized": boxed(ax["minimized"]),
            "accessibility": ax,
            "windowIdentityAmbiguous": owner.error != nil,
            "windowIdentityError": boxed(owner.error),
            "processImagePath": boxed(imageEvidence.path),
            "processImageSHA256": boxed(imageEvidence.sha256),
            "processImageRole": handoff.expectedProcessRole,
            "processImageVerificationSource": imageEvidence.source,
            "expectedProcessImagePath": boxed(expectedProcessPath),
            "expectedProcessImageSHA256": boxed(handoff.expectedProcessSHA256),
            "processIdentityMatches": imageEvidence.matches,
            "displayRelationship": targetDisplay,
            "covererOnExpectedDisplay": covererOnExpectedDisplay,
        ],
        "app": [
            "localizedName": boxed(app?.localizedName),
            "bundleIdentifier": boxed(app?.bundleIdentifier),
            "isActive": boxed(appActive),
            "isActiveSource": handoff.targetKind == "wine"
                ? "same-PID in-process AppKit telemetry" : "NSRunningApplication",
            "runningApplicationAvailable": app != nil,
            "isHidden": boxed(app?.isHidden),
            "isTerminated": boxed(app?.isTerminated),
            "activationPolicyRaw": boxed(app?.activationPolicy.rawValue),
        ],
        "inProcessTelemetry": telemetry,
        "inProcessProcessIdentity": [
            "verified": telemetryTail.processIdentityMatchesPinnedImage,
            "error": boxed(telemetryTail.identityError),
            "processIdentity": boxed(telemetryTail.processIdentity),
            "mainExecutableModuleIdentity": boxed(telemetryTail.wineMainExecutableIdentity),
        ],
        "windowsAboveTargetThatOverlap": above,
        "alphaOpaqueFullCoverCandidatePresent": opaqueCover,
        "neutralCovererProcesses": processRows,
        "neutralCovererWindows": covererWindows,
        "neutralCovererProcessPresent": processRowsResult == nil || !processRows.isEmpty,
        "neutralCovererWindowPresent": !covererWindows.isEmpty,
        "neutralCovererCoveringWindowPresent": covererWindows.contains { window in
            window["onScreen"] as? Bool == true
                && window["intersectsTarget"] as? Bool == true
                && (number(window["alpha"]) ?? 0) >= 0.99
                && integer(window["rankFrontToBack"]).map { $0 < (rank ?? Int.max) } == true
        },
        "activation": activation,
        "screenList": screenRows(),
    ]
    var completedSample = sample
    completedSample["canonicalUncoveredEligible"] = isCanonicalUncovered(completedSample)
    completedSample["coveredEligible"] = isCovered(completedSample)
    completedSample["restoredEligible"] = isRestored(completedSample)
    return (completedSample, windowID, owner.error)
}

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data + Data([0x0a]))
}

func main() throws -> Int32 {
    guard CommandLine.arguments.count == 9,
          let displayID = UInt32(CommandLine.arguments[7]) else {
        fputs("usage: public_window_observer <handoff.json> <complete.json> <result.json> <samples.jsonl> <latest.json> <coverer-path> <display-id> <trusted-runs-root>\n", stderr)
        return 2
    }
    let paths = Paths(
        handoff: URL(fileURLWithPath: CommandLine.arguments[1]),
        completion: URL(fileURLWithPath: CommandLine.arguments[2]),
        result: URL(fileURLWithPath: CommandLine.arguments[3]),
        samples: URL(fileURLWithPath: CommandLine.arguments[4]),
        latest: URL(fileURLWithPath: CommandLine.arguments[5]),
        coverer: URL(fileURLWithPath: CommandLine.arguments[6]),
        expectedDisplayID: displayID,
        runsRoot: URL(fileURLWithPath: CommandLine.arguments[8]).standardizedFileURL
    )
    guard sha256(paths.coverer) == expectedCovererSHA256 else {
        throw NSError(domain: "PublicWindowObserver", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "pinned neutral-coverer SHA-256 mismatch"])
    }
    let startedAtUTC = utcNow()
    let startedAtMonotonicNS = DispatchTime.now().uptimeNanoseconds
    var queriesBeforeHandoff: [[String: Any]] = []
    var samples: [[String: Any]] = []
    var activationActions: [[String: Any]] = []
    var handoff: TargetHandoff?
    var handoffReceivedAtUTC: String?
    var handoffReceivedMonotonicNS: UInt64?
    var firstTargetWindowDiscoveryUTC: String?
    var firstTargetWindowDiscoveryMonotonicNS: UInt64?
    var lastSampleAt: UInt64 = 0
    var previousWindowID: Int?
    var ambiguityError: String?
    var activationCount = 0
    var lastActivationAt: UInt64 = 0
    var completeReceipt: [String: Any]?
    var completeReceivedAtUTC: String?
    var completeReceivedMonotonicNS: UInt64?
    var watchdogTriggered = false
    var observerError: String?
    var telemetryTail: TelemetryTail?
    var targetProcessImageSHA256: String?
    var targetProcessImagePath: String?
    var targetProcessImageRole: String?
    let startWindowQueryAtMonotonicNS = DispatchTime.now().uptimeNanoseconds
    let startWindowRowsQuery = windowRows(.optionAll)
    let startWindowQueryCompletedAtMonotonicNS = DispatchTime.now().uptimeNanoseconds
    let covererProcessQueryAtStart = DispatchTime.now().uptimeNanoseconds
    let covererAtStartResult = covererProcesses(paths.coverer)
    let covererProcessQueryCompletedAtStart = DispatchTime.now().uptimeNanoseconds
    let covererAtStart = covererAtStartResult ?? []
    let startWindowRows = startWindowRowsQuery ?? []
    let covererWindowsAtStart = startWindowRows.enumerated().compactMap { index, row -> [String: Any]? in
        let owner = (row[kCGWindowOwnerName as String] as? String ?? "")
        let title = (row[kCGWindowName as String] as? String ?? "")
        guard owner.localizedCaseInsensitiveContains("neutral-coverer")
                || title.localizedCaseInsensitiveContains("neutral-coverer")
                || ownerPID(row).map(Int.init).map({ pid in covererAtStart.contains { integer($0["pid"]) == pid } }) == true
        else { return nil }
        return [
            "rank": index, "windowID": boxed(integer(row[kCGWindowNumber as String])),
            "ownerPID": boxed(ownerPID(row).map { Int($0) }),
            "ownerName": owner, "title": title,
            "onScreen": boolValue(row[kCGWindowIsOnscreen as String]) ?? false,
            "bounds": rectJSON(rect(row[kCGWindowBounds as String])),
            "layer": boxed(integer(row[kCGWindowLayer as String])),
            "alpha": boxed(number(row[kCGWindowAlpha as String])),
        ]
    }
    try writeJSONFirstOrReplace([
        "schemaVersion": schemaVersion, "event": "observer_started",
        "startedAtUTC": startedAtUTC, "startedAtMonotonicNS": startedAtMonotonicNS,
        "expectedDisplayID": Int(paths.expectedDisplayID),
        "startWindowQueryAtMonotonicNS": startWindowQueryAtMonotonicNS,
        "startWindowQueryCompletedAtMonotonicNS": startWindowQueryCompletedAtMonotonicNS,
        "startWindowListQuerySucceeded": startWindowRowsQuery != nil,
        "startWindowCount": startWindowRows.count,
        "covererProcessQueryAtStartMonotonicNS": covererProcessQueryAtStart,
        "covererProcessQueryCompletedAtStartMonotonicNS": covererProcessQueryCompletedAtStart,
        "covererProcessEnumerationSucceededAtStart": covererAtStartResult != nil,
        "covererProcessesAtStart": covererAtStart,
        "covererWindowsAtStart": covererWindowsAtStart,
        "canonicalUncovered": ["attained": false],
        "covered": ["attained": false], "restored": ["attained": false],
        "sampleCount": 0,
    ], to: paths.latest)
    emit([
        "event": "observer_started", "startedAtUTC": startedAtUTC,
        "startedAtMonotonicNS": startedAtMonotonicNS,
        "observerStartWindowQueryAtMonotonicNS": startWindowQueryAtMonotonicNS,
        "observerStartWindowQueryCompletedAtMonotonicNS": startWindowQueryCompletedAtMonotonicNS,
        "observerStartWindowListQuerySucceeded": startWindowRowsQuery != nil,
        "observerStartWindowCount": startWindowRows.count,
        "covererProcessQueryAtStartMonotonicNS": covererProcessQueryAtStart,
        "covererProcessQueryCompletedAtStartMonotonicNS": covererProcessQueryCompletedAtStart,
        "covererProcessEnumerationSucceededAtStart": covererAtStartResult != nil,
        "covererBinaryPath": paths.coverer.path,
        "covererBinarySHA256": expectedCovererSHA256,
        "covererProcessesAtStart": covererAtStart,
        "covererWindowsAtStart": covererWindowsAtStart,
        "watchdogSeconds": Double(safetyWatchdogNS) / 1_000_000_000,
    ])
    // Keep the durable startup marker observable after initial enumeration and writes.
    lastSampleAt = DispatchTime.now().uptimeNanoseconds

    while true {
        let now = DispatchTime.now().uptimeNanoseconds
        if now >= startedAtMonotonicNS && now - startedAtMonotonicNS > safetyWatchdogNS {
            watchdogTriggered = true
            observerError = "safety watchdog fired before lifecycle completion"
            break
        }
        if handoff == nil, let raw = readJSONObject(paths.handoff) {
            guard let parsed = parseHandoff(raw, runsRoot: paths.runsRoot) else {
                observerError = "target identity handoff or run-local trace path is malformed"
                break
            }
            guard sha256(parsed.expectedTargetImagePath) == parsed.expectedTargetImageSHA256 else {
                observerError = "expected target image identity hash does not match the current file"
                break
            }
            guard sha256(parsed.expectedProcessPath) == parsed.expectedProcessSHA256 else {
                observerError = "expected target process image hash does not match the pinned file"
                break
            }
            if parsed.targetKind == "native" {
                guard let currentTargetImagePath = processPath(parsed.pid) else {
                    observerError = "runner handed off a Native PID with no NSRunningApplication executable URL"
                    break
                }
                let actualProcessHash = sha256(URL(fileURLWithPath: currentTargetImagePath))
                guard URL(fileURLWithPath: currentTargetImagePath).standardizedFileURL.path
                        == parsed.expectedProcessPath.standardizedFileURL.path,
                      actualProcessHash == parsed.expectedProcessSHA256 else {
                    targetProcessImagePath = currentTargetImagePath
                    targetProcessImageSHA256 = actualProcessHash
                    targetProcessImageRole = parsed.expectedProcessRole
                    let actualHashDescription = actualProcessHash ?? "unavailable"
                    observerError = "handed-off Native PID process image mismatch: observed \(currentTargetImagePath) sha256=\(actualHashDescription), expected \(parsed.expectedProcessRole) \(parsed.expectedProcessPath.path) sha256=\(parsed.expectedProcessSHA256)"
                    break
                }
                targetProcessImageSHA256 = actualProcessHash
                targetProcessImagePath = currentTargetImagePath
                targetProcessImageRole = parsed.expectedProcessRole
            }
            handoff = parsed
            handoffReceivedAtUTC = utcNow()
            handoffReceivedMonotonicNS = now
            telemetryTail = TelemetryTail(
                path: parsed.targetTracePath, pid: parsed.pid, token: parsed.token,
                expectedProcessPath: parsed.expectedProcessPath.standardizedFileURL.path,
                expectedProcessSHA256: parsed.expectedProcessSHA256)
            let app = NSRunningApplication(processIdentifier: parsed.pid)
            let activation: [String: Any] = [
                "event": "activation", "atUTC": utcNow(),
                "atMonotonicNS": DispatchTime.now().uptimeNanoseconds,
                "targetPID": Int(parsed.pid),
                "source": app == nil ? "NSRunningApplication unavailable for handed-off PID" : "NSRunningApplication.activate",
                "runningApplicationAvailable": app != nil,
                "returnValue": boxed(app?.activate(options: [.activateAllWindows])),
            ]
            activationActions.append(activation)
            activationCount += 1
            lastActivationAt = DispatchTime.now().uptimeNanoseconds
            activationActions.append([
                "event": "activation", "atUTC": utcNow(),
                "atMonotonicNS": DispatchTime.now().uptimeNanoseconds,
                "targetPID": Int(parsed.pid), "source": "public Accessibility frontmost/raise",
                "result": raiseTargetWindow(pid: parsed.pid, expectedTitle: parsed.title),
            ])
            emit([
                "event": "target_handoff_received",
                "receivedAtUTC": handoffReceivedAtUTC as Any? ?? NSNull(),
                "receivedAtMonotonicNS": handoffReceivedMonotonicNS as Any? ?? NSNull(),
                "runID": parsed.runID, "experimentToken": parsed.token,
                "targetUnixPID": Int(parsed.pid), "runnerPID": parsed.runnerPID,
                "runnerMonotonicNS": parsed.runnerMonotonicNS,
                "targetKind": parsed.targetKind,
                "expectedTargetImagePath": parsed.expectedTargetImagePath.path,
                "expectedTargetImageSHA256": parsed.expectedTargetImageSHA256,
                "expectedTargetProcessImagePath": parsed.expectedProcessPath.path,
                "expectedTargetProcessImageRole": parsed.expectedProcessRole,
    "expectedTargetProcessImageSHA256": parsed.expectedProcessSHA256,
                "targetProcessImagePath": targetProcessImagePath as Any? ?? NSNull(),
                "targetProcessImageRole": targetProcessImageRole as Any? ?? NSNull(),
                "targetProcessImageSHA256": targetProcessImageSHA256 as Any? ?? NSNull(),
                "targetProcessImageVerificationSource": parsed.targetKind == "wine"
                    ? "same-PID same-token process_identity and startup_module_identity trace"
                    : "NSRunningApplication executableURL and pinned file SHA-256",
            ])
        }
        if let parsed = handoff {
            telemetryTail?.refresh()
            if let identityError = telemetryTail?.identityError {
                observerError = "target runtime identity mismatch: \(identityError)"
                break
            }
            if parsed.targetKind == "wine",
               telemetryTail?.processIdentityMatchesPinnedImage == true {
                targetProcessImagePath = telemetryTail?.processIdentity?["main_executable_path"] as? String
                targetProcessImageSHA256 = telemetryTail?.processIdentity?["main_executable_sha256"] as? String
                targetProcessImageRole = parsed.expectedProcessRole
            }
            if now - lastSampleAt >= UInt64(sampleIntervalSeconds * 1_000_000_000) {
                let queryStarted = DispatchTime.now().uptimeNanoseconds
                let allRowsResult = windowRows(.optionAll)
                let visibleRowsResult = windowRows(.optionOnScreenOnly)
                let queryCompleted = DispatchTime.now().uptimeNanoseconds
                let allRows = allRowsResult ?? []
                let visibleRows = visibleRowsResult ?? []
                let (sample, newWindowID, error) = makeSample(
                    parsed, paths: paths, allRows: allRows, visibleRows: visibleRows,
                    allWindowQuerySucceeded: allRowsResult != nil,
                    onScreenWindowQuerySucceeded: visibleRowsResult != nil,
                    queryStartedMonotonicNS: queryStarted,
                    queryCompletedMonotonicNS: queryCompleted,
                    telemetryTail: telemetryTail!,
                    processImageSHA256AtHandoff: targetProcessImageSHA256,
                    activation: [
                        "attemptCount": activationCount,
                        "lastActivationMonotonicNS": lastActivationAt,
                    ], identity: parsed.raw, previousWindowID: previousWindowID)
                samples.append(sample)
                try appendJSON(sample, to: paths.samples)
                lastSampleAt = now
                if firstTargetWindowDiscoveryMonotonicNS == nil,
                   (sample["target"] as? [String: Any])?["windowID"] as? Int != nil {
                    firstTargetWindowDiscoveryUTC = sample["sampledAtUTC"] as? String
                    firstTargetWindowDiscoveryMonotonicNS = sample["sampledAtMonotonicNS"] as? UInt64
                }
                if let error { ambiguityError = error }
                if newWindowID != nil { previousWindowID = newWindowID }
                if activationCount < 3, now > lastActivationAt + 2_000_000_000,
                   isCanonicalUncovered(sample) == false {
                    let retryApp = NSRunningApplication(processIdentifier: parsed.pid)
                    activationActions.append([
                        "event": "activation", "atUTC": utcNow(),
                        "atMonotonicNS": now, "targetPID": Int(parsed.pid),
                        "source": retryApp == nil
                            ? "NSRunningApplication unavailable for handed-off PID"
                            : "NSRunningApplication.activate retry",
                        "runningApplicationAvailable": retryApp != nil,
                        "returnValue": boxed(retryApp?.activate(options: [.activateAllWindows])),
                    ])
                    activationActions.append([
                        "event": "activation", "atUTC": utcNow(),
                        "atMonotonicNS": DispatchTime.now().uptimeNanoseconds,
                        "targetPID": Int(parsed.pid), "source": "public Accessibility frontmost/raise retry",
                        "result": raiseTargetWindow(pid: parsed.pid, expectedTitle: parsed.title),
                    ])
                    activationCount += 1
                    lastActivationAt = DispatchTime.now().uptimeNanoseconds
                }
                let uncovered = streakSummary(samples, predicate: isCanonicalUncovered)
                let covered = streakSummary(samples, predicate: { isCovered($0) })
                let restored = streakSummary(samples, predicate: isRestored)
                let latestState: [String: Any] = [
                    "schemaVersion": schemaVersion,
                    "latestSample": sample,
                    "canonicalUncovered": uncovered,
                    "covered": covered,
                    "restored": restored,
                    "sampleCount": samples.count,
                    "lastQueryAtUTC": sample["sampledAtUTC"] as Any? ?? NSNull(),
                    "lastQueryMonotonicNS": sample["sampledAtMonotonicNS"] as Any? ?? NSNull(),
                ]
                try writeJSONFirstOrReplace(latestState, to: paths.latest)
                try writeJSONFirstOrReplace(latestState, to: paths.result)
                emit(["event": "sample", "sampleCount": samples.count,
                      "sampledAtUTC": sample["sampledAtUTC"] as Any? ?? NSNull(),
                      "targetWindowID": (sample["target"] as? [String: Any])?["windowID"] as Any? ?? NSNull(),
                      "canonicalUncoveredAttained": uncovered["attained"] ?? false,
                      "coveredAttained": covered["attained"] ?? false,
                      "restoredAttained": restored["attained"] ?? false])
            }
        } else if now - lastSampleAt >= UInt64(sampleIntervalSeconds * 1_000_000_000) {
            let queryStarted = DispatchTime.now().uptimeNanoseconds
            let allRows = windowRows(.optionAll)
            let visibleRows = windowRows(.optionOnScreenOnly)
            let queryCompleted = DispatchTime.now().uptimeNanoseconds
            let covererProcessQueryStarted = DispatchTime.now().uptimeNanoseconds
            let processesResult = covererProcesses(paths.coverer)
            let covererProcessQueryCompleted = DispatchTime.now().uptimeNanoseconds
            let processes = processesResult ?? []
            let attempt: [String: Any] = [
                "event": "query_attempt_before_target_handoff",
                "queryAtUTC": utcNow(),
                "queryAtMonotonicNS": now,
                "queryStartedMonotonicNS": queryStarted,
                "queryCompletedMonotonicNS": queryCompleted,
                "allWindowListQuerySucceeded": allRows != nil,
                "onScreenWindowListQuerySucceeded": visibleRows != nil,
                "covererProcessEnumerationSucceeded": processesResult != nil,
                "covererProcessQueryStartedMonotonicNS": covererProcessQueryStarted,
                "covererProcessQueryCompletedMonotonicNS": covererProcessQueryCompleted,
                "allWindowCount": allRows?.count as Any? ?? NSNull(),
                "onScreenWindowCount": visibleRows?.count as Any? ?? NSNull(),
                "targetOwnerPID": NSNull(),
                "targetOwnerWindowCount": 0,
                "covererProcesses": processes,
                "covererWindows": (allRows ?? []).enumerated().compactMap { index, row -> [String: Any]? in
                    let owner = (row[kCGWindowOwnerName as String] as? String ?? "")
                    let title = (row[kCGWindowName as String] as? String ?? "")
                    guard owner.localizedCaseInsensitiveContains("neutral-coverer")
                            || title.localizedCaseInsensitiveContains("neutral-coverer")
                            || ownerPID(row).map(Int.init).map({ pid in processes.contains { integer($0["pid"]) == pid } }) == true
                    else { return nil }
                    return [
                        "rank": index, "windowID": boxed(integer(row[kCGWindowNumber as String])),
                        "ownerPID": boxed(ownerPID(row).map { Int($0) }),
                        "ownerName": owner, "title": title,
                        "onScreen": boolValue(row[kCGWindowIsOnscreen as String]) ?? false,
                        "bounds": rectJSON(rect(row[kCGWindowBounds as String])),
                        "layer": boxed(integer(row[kCGWindowLayer as String])),
                        "alpha": boxed(number(row[kCGWindowAlpha as String])),
                    ]
                },
            ]
            queriesBeforeHandoff.append(attempt)
            try appendJSON(attempt, to: paths.samples)
            lastSampleAt = now
            let latestState: [String: Any] = [
                "schemaVersion": schemaVersion, "event": "observer_started",
                "observerState": "WAITING_FOR_TARGET_HANDOFF",
                "startedAtUTC": startedAtUTC,
                "startedAtMonotonicNS": startedAtMonotonicNS,
                "latestQueryAttempt": attempt,
                "canonicalUncovered": ["attained": false],
                "covered": ["attained": false], "restored": ["attained": false],
                "sampleCount": 0, "queriesBeforeTargetHandoff": queriesBeforeHandoff.count,
            ]
            try writeJSONFirstOrReplace(latestState, to: paths.latest)
        }

        if completeReceipt == nil, let raw = readJSONObject(paths.completion) {
            guard let parsed = handoff else {
                observerError = "experiment completed before a valid target PID handoff"
                break
            }
            let completionMatches = raw["run_id"] as? String == parsed.runID
                && raw["experiment_token"] as? String == parsed.token
                && integer(raw["target_unix_pid"]) == Int(parsed.pid)
            if let runID = raw["run_id"] as? String, completionMatches {
                completeReceipt = raw
                completeReceivedAtUTC = utcNow()
                completeReceivedMonotonicNS = DispatchTime.now().uptimeNanoseconds
                emit(["event": "experiment_complete_received",
                      "receivedAtUTC": completeReceivedAtUTC as Any? ?? NSNull(),
                      "receivedAtMonotonicNS": completeReceivedMonotonicNS as Any? ?? NSNull(),
                      "runID": runID, "status": raw["status"] as Any? ?? NSNull()])
            } else {
                observerError = "experiment-complete signal identity does not match the target handoff"
                break
            }
        }
        if completeReceipt != nil { break }
        Thread.sleep(forTimeInterval: sampleIntervalSeconds)
    }

    let endedAtUTC = utcNow()
    let endedAtMonotonicNS = DispatchTime.now().uptimeNanoseconds
    let uncovered = streakSummary(samples, predicate: isCanonicalUncovered)
    let covered = streakSummary(samples, predicate: { isCovered($0) })
    let coveredProofNS = (covered["attained"] as? Bool == true)
        ? (covered["stabilityProvenMonotonicNS"] as? UInt64) : nil
    let restoredSamples = coveredProofNS.map { proof in
        samples.filter { sample in
            guard let stamp = sample["sampledAtMonotonicNS"] as? UInt64 else { return false }
            return stamp > proof
        }
    } ?? []
    let restored = streakSummary(restoredSamples, predicate: isRestored)
    let covererBeforeUncovered = samples.contains { sample in
        (sample["neutralCovererProcessPresent"] as? Bool == true)
            || (sample["neutralCovererWindowPresent"] as? Bool == true)
    }
    let result: [String: Any] = [
        "schemaVersion": schemaVersion,
        "observer": "public CGWindowListCopyWindowInfo and Accessibility APIs; NSRunningApplication executableURL for Native identity; same-PID token-bound Wine runtime trace identity",
        "startedAtUTC": startedAtUTC,
        "startedAtMonotonicNS": startedAtMonotonicNS,
        "targetHandoffReceivedAtUTC": boxed(handoffReceivedAtUTC),
        "targetHandoffReceivedAtMonotonicNS": boxed(handoffReceivedMonotonicNS),
        "targetFirstWindowDiscoveryAtUTC": boxed(firstTargetWindowDiscoveryUTC),
        "targetFirstWindowDiscoveryAtMonotonicNS": boxed(firstTargetWindowDiscoveryMonotonicNS),
        "targetPID": boxed(handoff.map { Int($0.pid) }),
        "targetKind": boxed(handoff?.targetKind),
        "windowTelemetryExpected": boxed(handoff?.telemetryExpected),
        "targetRunID": boxed(handoff?.runID),
        "experimentToken": boxed(handoff?.token),
        "targetProcessImagePath": boxed(targetProcessImagePath),
        "targetProcessImageRole": boxed(targetProcessImageRole),
        "targetProcessImageSHA256": boxed(targetProcessImageSHA256),
        "targetProcessIdentityVerified": handoff?.targetKind == "wine"
            ? (telemetryTail?.processIdentityMatchesPinnedImage ?? false)
            : (targetProcessImagePath != nil && targetProcessImageSHA256 != nil),
        "targetProcessImageVerificationSource": handoff?.targetKind == "wine"
            ? "same-PID same-token process_identity and startup_module_identity trace"
            : "NSRunningApplication executableURL and pinned file SHA-256",
        "targetProcessIdentityEvidence": [
            "verified": handoff?.targetKind == "wine"
                ? (telemetryTail?.processIdentityMatchesPinnedImage ?? false)
                : (targetProcessImagePath != nil && targetProcessImageSHA256 != nil),
            "source": handoff?.targetKind == "wine"
                ? "same-PID same-token process_identity and startup_module_identity trace"
                : "NSRunningApplication executableURL and pinned file SHA-256",
            "path": boxed(targetProcessImagePath),
            "sha256": boxed(targetProcessImageSHA256),
            "processIdentity": boxed(telemetryTail?.processIdentity),
            "mainExecutableModuleIdentity": boxed(telemetryTail?.wineMainExecutableIdentity),
        ],
        "targetWindowID": boxed(previousWindowID),
        "targetIdentityHandoff": boxed(handoff?.raw),
        "expectedDisplayID": Int(paths.expectedDisplayID),
        "activationActions": activationActions,
        "observerQueriesBeforeTargetHandoff": queriesBeforeHandoff,
        "observerQueryAttemptsBeforeTargetWindow": samples.filter {
            (integer(($0["target"] as? [String: Any])?["windowID"]) ?? 0) == 0
        },
        "sampleCount": samples.count,
        "samples": samples,
        "covererBinaryPath": paths.coverer.path,
        "covererBinarySHA256": expectedCovererSHA256,
        "covererProcessesAtObserverStart": covererAtStart,
        "covererWindowsAtObserverStart": covererWindowsAtStart,
        "covererProcessEnumerationSucceededAtObserverStart": covererAtStartResult != nil,
        "observerStartWindowQuerySucceeded": startWindowRowsQuery != nil,
        "canonicalUncoveredState": uncovered,
        "coveredState": covered,
        "restoredState": restored,
        "neutralCovererOrWindowPresentDuringObservedSamples": covererBeforeUncovered,
        "targetWindowIdentityAmbiguity": boxed(ambiguityError),
        "experimentCompleteSignal": boxed(completeReceipt),
        "experimentCompleteReceivedAtUTC": boxed(completeReceivedAtUTC),
        "experimentCompleteReceivedAtMonotonicNS": boxed(completeReceivedMonotonicNS),
        "watchdogTriggered": watchdogTriggered,
        "error": boxed(observerError),
        "endedAtUTC": endedAtUTC,
        "endedAtMonotonicNS": endedAtMonotonicNS,
        "elapsedMonotonicNS": endedAtMonotonicNS >= startedAtMonotonicNS
            ? endedAtMonotonicNS - startedAtMonotonicNS : NSNull() as Any,
    ]
    try writeJSONFirstOrReplace(result, to: paths.result)
    try writeJSONFirstOrReplace(result, to: paths.latest)
    emit(["event": "observer_finished", "endedAtUTC": endedAtUTC,
          "endedAtMonotonicNS": endedAtMonotonicNS,
          "sampleCount": samples.count,
          "canonicalUncoveredAttained": uncovered["attained"] ?? false,
          "coveredAttained": covered["attained"] ?? false,
          "restoredAttained": restored["attained"] ?? false,
          "watchdogTriggered": watchdogTriggered,
          "error": observerError as Any? ?? NSNull()])
    return observerError == nil && !watchdogTriggered ? 0 : 1
}

do {
    exit(try main())
} catch {
    fputs("public window observer failed: \(error)\n", stderr)
    exit(1)
}
