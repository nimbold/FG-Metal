import AppKit
import CoreGraphics
import Darwin
import Dispatch
import Foundation

private func utcNow() -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: Date())
}

private func readJSONObject(_ url: URL) -> [String: Any]? {
    guard let data = try? Data(contentsOf: url),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return nil
    }
    return object
}

private func appendJSON(_ object: [String: Any], to handle: FileHandle) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    handle.write(data)
    handle.write(Data([0x0a]))
}

private func jsonRect(_ value: Any?) -> Any {
    guard let dict = value as? [String: Any],
          let x = (dict["X"] as? NSNumber)?.doubleValue,
          let y = (dict["Y"] as? NSNumber)?.doubleValue,
          let width = (dict["Width"] as? NSNumber)?.doubleValue,
          let height = (dict["Height"] as? NSNumber)?.doubleValue else {
        return NSNull()
    }
    return ["x": x, "y": y, "width": width, "height": height]
}

private func displayRows() -> [[String: Any]] {
    NSScreen.screens.map { screen in
        let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
        let bounds = id.map { CGDisplayBounds(CGDirectDisplayID($0)) }
        func rect(_ value: CGRect?) -> Any {
            guard let value else { return NSNull() }
            return ["x": value.origin.x, "y": value.origin.y,
                    "width": value.size.width, "height": value.size.height]
        }
        return [
            "display_id": id.map { Int($0) } as Any? ?? NSNull(),
            "name": screen.localizedName,
            "is_main": screen == NSScreen.main,
            "frame_appkit": rect(screen.frame),
            "visible_frame_appkit": rect(screen.visibleFrame),
            "bounds_cg": rect(bounds),
            "backing_scale": screen.backingScaleFactor,
        ]
    }
}

private func ownerPID(_ row: [String: Any]) -> pid_t? {
    (row[kCGWindowOwnerPID as String] as? NSNumber).map { pid_t($0.int32Value) }
}

private func rows(_ option: CGWindowListOption) -> [[String: Any]]? {
    CGWindowListCopyWindowInfo([option, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
}

private func candidateRow(_ row: [String: Any], allRank: Int,
                          onscreenRank: Int?, targetPID: pid_t) -> [String: Any] {
    let windowID = (row[kCGWindowNumber as String] as? NSNumber)?.intValue
    let ownerName = row[kCGWindowOwnerName as String] as? String
    let title = row[kCGWindowName as String] as? String
    let layer = (row[kCGWindowLayer as String] as? NSNumber)?.intValue
    let alpha = (row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue
    let onscreen = row[kCGWindowIsOnscreen as String] as? Bool
    return [
        "cg_window_id": windowID as Any? ?? NSNull(),
        "owner_pid": ownerPID(row).map { Int($0) } as Any? ?? NSNull(),
        "expected_owner_pid": Int(targetPID),
        "owner_name": ownerName as Any? ?? NSNull(),
        "title": title as Any? ?? NSNull(),
        "bounds_cg": jsonRect(row[kCGWindowBounds as String]),
        "layer": layer as Any? ?? NSNull(),
        "alpha": alpha as Any? ?? NSNull(),
        "onscreen": onscreen as Any? ?? NSNull(),
        "rank_all_windows": allRank,
        "rank_onscreen_windows": onscreenRank as Any? ?? NSNull(),
    ]
}

private func processIsAlive(_ pid: pid_t) -> Bool {
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

private func matchingCompletion(_ url: URL, runID: String, token: String,
                                pid: pid_t) -> Bool {
    guard let completion = readJSONObject(url) else { return false }
    return completion["run_id"] as? String == runID
        && completion["experiment_token"] as? String == token
        && (completion["target_unix_pid"] as? NSNumber)?.int32Value == pid
}

private func run() throws -> Int32 {
    guard CommandLine.arguments.count == 5 else {
        fputs("usage: public_cgwindow_inventory <handoff.json> <completion.json> <output.jsonl> <interval-ms>\n", stderr)
        return 2
    }
    let handoffURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let completionURL = URL(fileURLWithPath: CommandLine.arguments[2])
    let outputURL = URL(fileURLWithPath: CommandLine.arguments[3])
    guard let intervalMS = UInt64(CommandLine.arguments[4]), intervalMS >= 100 else {
        fputs("poll interval must be an integer of at least 100 ms\n", stderr)
        return 2
    }
    guard !FileManager.default.fileExists(atPath: outputURL.path),
          FileManager.default.createFile(atPath: outputURL.path, contents: nil,
                                         attributes: [.posixPermissions: 0o600]) else {
        fputs("refusing to replace an existing or uncreatable output file\n", stderr)
        return 2
    }
    let output = try FileHandle(forWritingTo: outputURL)
    defer { try? output.close() }

    let handoffDeadline = Date().addingTimeInterval(180)
    var handoff: [String: Any]?
    while Date() < handoffDeadline {
        if let value = readJSONObject(handoffURL) { handoff = value; break }
        if FileManager.default.fileExists(atPath: completionURL.path) {
            try appendJSON(["event": "poller_result", "status": "completed_without_target_handoff",
                            "finished_at_utc": utcNow()], to: output)
            return 1
        }
        Thread.sleep(forTimeInterval: 0.02)
    }
    guard let handoff,
          let pidNumber = handoff["target_unix_pid"] as? NSNumber,
          pidNumber.int32Value > 0,
          let runID = handoff["run_id"] as? String,
          let token = handoff["experiment_token"] as? String else {
        try appendJSON(["event": "poller_result", "status": "invalid_or_missing_target_handoff",
                        "handoff_path": handoffURL.path, "finished_at_utc": utcNow()], to: output)
        return 1
    }
    let pid = pid_t(pidNumber.int32Value)
    let started = DispatchTime.now().uptimeNanoseconds
    try appendJSON([
        "event": "capture_start",
        "started_at_utc": utcNow(),
        "started_monotonic_ns": started,
        "run_id": runID,
        "experiment_token": token,
        "target_unix_pid": Int(pid),
        "handoff_path": handoffURL.path,
        "display_inventory": displayRows(),
        "poll_interval_ms": intervalMS,
    ], to: output)

    var sampleIndex = 0
    var completedDeadSamples = 0
    let captureDeadline = Date().addingTimeInterval(180)
    let interval = Double(intervalMS) / 1000.0
    while Date() < captureDeadline {
        let queryStart = DispatchTime.now().uptimeNanoseconds
        let all = rows(.optionAll)
        let onscreen = rows(.optionOnScreenOnly)
        let queryEnd = DispatchTime.now().uptimeNanoseconds
        let onscreenRanks: [Int: Int] = Dictionary(uniqueKeysWithValues:
            (onscreen ?? []).enumerated().compactMap { rank, row in
                guard ownerPID(row) != nil,
                      let id = (row[kCGWindowNumber as String] as? NSNumber)?.intValue else { return nil }
                return (id, rank)
            })
        let owned: [[String: Any]] = (all ?? []).enumerated().compactMap { rank, row in
            guard ownerPID(row) == pid else { return nil }
            let id = (row[kCGWindowNumber as String] as? NSNumber)?.intValue
            return candidateRow(row, allRank: rank,
                                onscreenRank: id.flatMap { onscreenRanks[$0] }, targetPID: pid)
        }
        try appendJSON([
            "event": "sample",
            "sample_index": sampleIndex,
            "sampled_at_utc": utcNow(),
            "query_started_monotonic_ns": queryStart,
            "query_completed_monotonic_ns": queryEnd,
            "all_window_query_succeeded": all != nil,
            "onscreen_window_query_succeeded": onscreen != nil,
            "all_window_count": all?.count as Any? ?? NSNull(),
            "onscreen_window_count": onscreen?.count as Any? ?? NSNull(),
            "target_unix_pid": Int(pid),
            "run_id": runID,
            "experiment_token": token,
            "same_pid_window_count": owned.count,
            "windows": owned,
        ], to: output)
        sampleIndex += 1

        let completionMatches = matchingCompletion(completionURL, runID: runID,
                                                   token: token, pid: pid)
        if completionMatches && !processIsAlive(pid) {
            completedDeadSamples += 1
            if completedDeadSamples >= 3 { break }
        } else {
            completedDeadSamples = 0
        }
        Thread.sleep(forTimeInterval: interval)
    }
    let ended = DispatchTime.now().uptimeNanoseconds
    let completionObserved = matchingCompletion(completionURL, runID: runID,
                                                token: token, pid: pid)
    try appendJSON([
        "event": "poller_result",
        "status": completionObserved && !processIsAlive(pid) ? "PASS" : "INCOMPLETE",
        "finished_at_utc": utcNow(),
        "finished_monotonic_ns": ended,
        "run_id": runID,
        "experiment_token": token,
        "target_unix_pid": Int(pid),
        "sample_count": sampleIndex,
        "matching_completion_observed": completionObserved,
        "target_pid_still_alive": processIsAlive(pid),
    ], to: output)
    return completionObserved && !processIsAlive(pid) ? 0 : 1
}

exit(try run())
