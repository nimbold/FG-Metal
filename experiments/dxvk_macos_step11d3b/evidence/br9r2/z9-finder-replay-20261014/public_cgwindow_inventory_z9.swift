import AppKit
import CoreGraphics
import Darwin
import Dispatch
import Foundation

private let maxCoverCandidatesPerSample = 64
private let maxCoverCandidateRowBytes = 192
private let maxCoverEvidenceBytesPerSample = 64 * 192 + 512

private let machTimebaseInfo: mach_timebase_info_data_t = {
    var info = mach_timebase_info_data_t()
    _ = mach_timebase_info(&info)
    return info
}()

private func machAbsoluteTimeNS() -> UInt64 {
    let numerator = UInt64(machTimebaseInfo.numer)
    let denominator = UInt64(machTimebaseInfo.denom)
    guard numerator > 0, denominator > 0 else { return 0 }
    let ticks = mach_absolute_time()
    return (ticks / denominator) * numerator
        + ((ticks % denominator) * numerator) / denominator
}

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

private func appendJSON(_ object: [String: Any], to handle: FileHandle,
                        maximumFileBytes: UInt64? = nil) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    let line = data + Data([0x0a])
    if let maximumFileBytes {
        let currentOffset = try handle.offset()
        guard currentOffset <= maximumFileBytes,
              UInt64(line.count) <= maximumFileBytes - currentOffset else {
            throw NSError(domain: "Z9CollectorOutputBudget", code: 1,
                          userInfo: [NSLocalizedDescriptionKey:
                            "WindowServer evidence reached its derived JSONL byte cap"])
        }
    }
    try handle.write(contentsOf: line)
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

private func cgRect(_ row: [String: Any]) -> CGRect? {
    guard let dict = row[kCGWindowBounds as String] as? [String: Any],
          let x = (dict["X"] as? NSNumber)?.doubleValue,
          let y = (dict["Y"] as? NSNumber)?.doubleValue,
          let width = (dict["Width"] as? NSNumber)?.doubleValue,
          let height = (dict["Height"] as? NSNumber)?.doubleValue,
          width > 0, height > 0 else { return nil }
    return CGRect(x: x, y: y, width: width, height: height)
}

private func fullyContains(_ outer: CGRect, _ inner: CGRect) -> Bool {
    outer.minX <= inner.minX && outer.minY <= inner.minY
        && outer.maxX >= inner.maxX && outer.maxY >= inner.maxY
}

private func sameQueryCoverEvidence(allRows: [[String: Any]]?,
                                    onscreenRows: [[String: Any]]?,
                                    targetPID: pid_t,
                                    targetTitle: String) -> [String: Any] {
    guard let allRows, let onscreenRows else {
        return ["target_join_status": "UNKNOWN", "cover_evidence_complete": false,
                "opaque_full_cover_state": "UNKNOWN", "opaque_full_cover_rows": [],
                "unclassifiable_front_window_count": NSNull(),
                "cover_candidate_overflow": false]
    }
    let targets = allRows.filter {
        ownerPID($0) == targetPID
            && ($0[kCGWindowName as String] as? String) == targetTitle
    }
    guard targets.count == 1,
          let targetID = (targets[0][kCGWindowNumber as String] as? NSNumber)?.intValue,
          let targetBounds = cgRect(targets[0]) else {
        return ["target_join_status": "NO_UNIQUE_ALL_WINDOWS_TARGET", "cover_evidence_complete": false,
                "opaque_full_cover_state": "UNKNOWN", "opaque_full_cover_rows": [],
                "unclassifiable_front_window_count": NSNull(),
                "cover_candidate_overflow": false]
    }
    let targetOnscreen = onscreenRows.enumerated().filter { _, row in
        ownerPID(row) == targetPID
            && (row[kCGWindowNumber as String] as? NSNumber)?.intValue == targetID
    }
    guard targetOnscreen.count == 1 else {
        return ["target_join_status": "NO_UNIQUE_ONSCREEN_JOIN", "cover_evidence_complete": false,
                "opaque_full_cover_state": "UNKNOWN", "opaque_full_cover_rows": [],
                "unclassifiable_front_window_count": NSNull(),
                "cover_candidate_overflow": false]
    }

    let targetRank = targetOnscreen[0].offset
    var unclassifiableFrontWindowCount = 0
    var candidates: [[Any]] = []
    var candidateOverflow = false
    for (rank, row) in onscreenRows.enumerated() where rank < targetRank {
        let windowID = (row[kCGWindowNumber as String] as? NSNumber)?.intValue
        let owner = ownerPID(row)
        let bounds = cgRect(row)
        let alpha = (row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue
        guard let windowID, let owner, let bounds, let alpha else {
            unclassifiableFrontWindowCount += 1
            continue
        }
        guard alpha >= 0.999, fullyContains(bounds, targetBounds) else { continue }
        let layer = (row[kCGWindowLayer as String] as? NSNumber)?.intValue
        let encoded: [Any] = [windowID, Int(owner), bounds.origin.x, bounds.origin.y,
                              bounds.size.width, bounds.size.height,
                              layer as Any? ?? NSNull(), alpha, rank]
        let encodedBytes = try? JSONSerialization.data(withJSONObject: encoded).count
        guard candidates.count < maxCoverCandidatesPerSample,
              let encodedBytes, encodedBytes + 1 <= maxCoverCandidateRowBytes else {
            candidateOverflow = true
            continue
        }
        candidates.append(encoded)
    }
    let complete = unclassifiableFrontWindowCount == 0 && !candidateOverflow
    let state = !complete ? "UNKNOWN" : (candidates.isEmpty ? "CLEAR" : "COVERED")
    var evidence: [String: Any] = [
        "target_join_status": "PASS",
        "target_cg_window_id": targetID,
        "target_onscreen_rank": targetRank,
        "cover_evidence_complete": complete,
        "opaque_full_cover_state": state,
        "opaque_full_cover_rows": candidates,
        "unclassifiable_front_window_count": unclassifiableFrontWindowCount,
        "cover_candidate_overflow": candidateOverflow,
    ]
    if let encodedEvidence = try? JSONSerialization.data(withJSONObject: evidence),
       encodedEvidence.count > maxCoverEvidenceBytesPerSample {
        evidence["cover_evidence_complete"] = false
        evidence["opaque_full_cover_state"] = "UNKNOWN"
        evidence["opaque_full_cover_rows"] = []
        evidence["cover_candidate_overflow"] = true
    }
    return evidence
}

private func desktopPreflightEvidence(allRows: [[String: Any]]?,
                                      onscreenRows: [[String: Any]]?,
                                      queryStartMach: UInt64,
                                      queryEndMach: UInt64) -> [String: Any] {
    let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
    let loginDone = (session[kCGSessionLoginDoneKey as String] as? NSNumber)?.boolValue
    let onConsole = (session[kCGSessionOnConsoleKey as String] as? NSNumber)?.boolValue
    let userID = (session[kCGSessionUserIDKey as String] as? NSNumber)?.intValue
    let frontmostName = NSWorkspace.shared.frontmostApplication?.localizedName
    let frontmostLower = frontmostName?.lowercased() ?? ""
    let lockNameSuspected = frontmostLower.contains("loginwindow")
        || frontmostLower.contains("screensaver")
        || frontmostLower.contains("screen saver")
    let displayBounds = CGDisplayBounds(CGDirectDisplayID(1))
    let expectedTarget = CGRect(x: 68, y: 62, width: 640, height: 392)
    let targetInsideDisplay = displayBounds.width > 0 && displayBounds.height > 0
        && fullyContains(displayBounds, expectedTarget)
    var opaqueCandidates: [[Any]] = []
    var unclassifiableCount = 0
    var candidateOverflow = false
    var lockOwnerSuspected = false
    if let onscreenRows {
        for (rank, row) in onscreenRows.enumerated() {
            let ownerName = (row[kCGWindowOwnerName as String] as? String)?.lowercased() ?? ""
            if ownerName.contains("loginwindow") || ownerName.contains("screensaver")
                || ownerName.contains("screen saver") {
                lockOwnerSuspected = true
            }
            guard let bounds = cgRect(row),
                  let alpha = (row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue else {
                unclassifiableCount += 1
                continue
            }
            guard alpha >= 0.999, fullyContains(bounds, expectedTarget) else { continue }
            let cgID = (row[kCGWindowNumber as String] as? NSNumber)?.intValue ?? -1
            let pid = ownerPID(row).map { Int($0) } ?? -1
            let layer = (row[kCGWindowLayer as String] as? NSNumber)?.intValue
            let clippedOwner = String((row[kCGWindowOwnerName as String] as? String ?? "unknown").prefix(64))
            let encoded: [Any] = [cgID, pid, bounds.origin.x, bounds.origin.y,
                                  bounds.size.width, bounds.size.height,
                                  layer as Any? ?? NSNull(), alpha, rank, clippedOwner]
            let encodedBytes = try? JSONSerialization.data(withJSONObject: encoded).count
            guard opaqueCandidates.count < maxCoverCandidatesPerSample,
                  let encodedBytes, encodedBytes + 1 <= maxCoverCandidateRowBytes else {
                candidateOverflow = true
                continue
            }
            opaqueCandidates.append(encoded)
        }
    }
    let sessionCurrent = loginDone == true && onConsole == true && userID == Int(getuid())
    let queryComplete = allRows != nil && onscreenRows != nil
    let evidenceComplete = queryComplete && sessionCurrent && frontmostName != nil
        && targetInsideDisplay && unclassifiableCount == 0 && !candidateOverflow
    let status = evidenceComplete && !lockNameSuspected && !lockOwnerSuspected
        && opaqueCandidates.isEmpty ? "PASS" : "BLOCKED"
    var result: [String: Any] = [
        "event": "desktop_preflight",
        "status": status,
        "query_started_mach_absolute_ns": queryStartMach,
        "query_completed_mach_absolute_ns": queryEndMach,
        "all_window_query_succeeded": allRows != nil,
        "onscreen_window_query_succeeded": onscreenRows != nil,
        "session_login_complete": loginDone as Any? ?? NSNull(),
        "session_on_console": onConsole as Any? ?? NSNull(),
        "session_user_id": userID as Any? ?? NSNull(),
        "expected_user_id": Int(getuid()),
        "frontmost_application_name": frontmostName as Any? ?? NSNull(),
        "lock_screen_name_suspected": lockNameSuspected,
        "lock_screen_owner_suspected": lockOwnerSuspected,
        "unlock_evidence_source": "console session login complete; frontmost app present; no loginwindow or screensaver front owner",
        "expected_display_id": 1,
        "expected_target_bounds_cg": ["x": 68, "y": 62, "width": 640, "height": 392],
        "display_bounds_cg": ["x": displayBounds.origin.x, "y": displayBounds.origin.y,
                              "width": displayBounds.size.width, "height": displayBounds.size.height],
        "expected_target_inside_display": targetInsideDisplay,
        "opaque_full_cover_candidates": opaqueCandidates,
        "unclassifiable_onscreen_window_count": unclassifiableCount,
        "candidate_overflow": candidateOverflow,
        "evidence_complete": evidenceComplete,
    ]
    if let encoded = try? JSONSerialization.data(withJSONObject: result),
       encoded.count > maxCoverEvidenceBytesPerSample {
        result["status"] = "BLOCKED"
        result["evidence_complete"] = false
        result["candidate_overflow"] = true
        result["opaque_full_cover_candidates"] = []
    }
    return result
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

private struct WindowIdentity: Hashable {
    let ownerPID: Int32
    let cgWindowID: Int
}

private func windowIdentity(_ row: [String: Any]) -> WindowIdentity? {
    guard let pid = ownerPID(row),
          let id = (row[kCGWindowNumber as String] as? NSNumber)?.intValue else { return nil }
    return WindowIdentity(ownerPID: Int32(pid), cgWindowID: id)
}

private func candidateRow(_ row: [String: Any], allRank: Int?,
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
        "rank_all_windows": allRank as Any? ?? NSNull(),
        "rank_onscreen_windows": onscreenRank as Any? ?? NSNull(),
        "on_screen_joined": onscreenRank != nil,
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
    guard CommandLine.arguments.count == 7 else {
        fputs("usage: public_cgwindow_inventory_z9 <handoff.json> <completion.json> <output.jsonl> <interval-ms> <finder-pid> <max-output-bytes>\n", stderr)
        return 2
    }
    let handoffURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let completionURL = URL(fileURLWithPath: CommandLine.arguments[2])
    let outputURL = URL(fileURLWithPath: CommandLine.arguments[3])
    guard let intervalMS = UInt64(CommandLine.arguments[4]),
          intervalMS >= 50, intervalMS <= 1000,
          let finderPIDValue = Int32(CommandLine.arguments[5]), finderPIDValue > 0,
          let maximumOutputBytes = UInt64(CommandLine.arguments[6]), maximumOutputBytes > 0 else {
        fputs("poll interval, Finder PID, and output byte limit are invalid\n", stderr)
        return 2
    }
    let finderPID = pid_t(finderPIDValue)
    guard !FileManager.default.fileExists(atPath: outputURL.path),
          FileManager.default.createFile(atPath: outputURL.path, contents: nil,
                                         attributes: [.posixPermissions: 0o600]) else {
        fputs("refusing to replace an existing or uncreatable output file\n", stderr)
        return 2
    }
    let output = try FileHandle(forWritingTo: outputURL)
    defer { try? output.close() }

    // Capture the controlled Finder-window creation before the Wine target handoff,
    // then retain the same Finder PID throughout the Wine lifecycle.
    let handoffDeadline = DispatchTime.now().uptimeNanoseconds + 1_400_000_000_000
    let interval = UInt64(intervalMS) * 1_000_000
    var preHandoffSampleIndex = 0
    var preHandoffNextDeadline = DispatchTime.now().uptimeNanoseconds
    var preHandoffPreviousStart: UInt64?
    var preHandoffSkipped = 0
    var desktopPreflightWritten = false
    var handoff: [String: Any]?
    while DispatchTime.now().uptimeNanoseconds < handoffDeadline {
        let now = DispatchTime.now().uptimeNanoseconds
        if now >= preHandoffNextDeadline {
            let sampleStart = DispatchTime.now().uptimeNanoseconds
            let sampleStartMach = machAbsoluteTimeNS()
            let queryStart = DispatchTime.now().uptimeNanoseconds
            let queryStartMach = machAbsoluteTimeNS()
            let all = rows(.optionAll)
            let allEnd = DispatchTime.now().uptimeNanoseconds
            let allEndMach = machAbsoluteTimeNS()
            let onscreen = rows(.optionOnScreenOnly)
            let queryEnd = DispatchTime.now().uptimeNanoseconds
            let queryEndMach = machAbsoluteTimeNS()
            var finderAllRanks: [WindowIdentity: Int] = [:]
            for (rank, row) in (all ?? []).enumerated() where ownerPID(row) == finderPID {
                if let identity = windowIdentity(row) { finderAllRanks[identity] = rank }
            }
            var finderOnscreenRanks: [WindowIdentity: Int] = [:]
            for (rank, row) in (onscreen ?? []).enumerated() where ownerPID(row) == finderPID {
                if let identity = windowIdentity(row) { finderOnscreenRanks[identity] = rank }
            }
            let finderAll: [[String: Any]] = (all ?? []).enumerated().compactMap { rank, row in
                guard ownerPID(row) == finderPID else { return nil }
                let identity = windowIdentity(row)
                return candidateRow(row, allRank: rank,
                                    onscreenRank: identity.flatMap { finderOnscreenRanks[$0] },
                                    targetPID: finderPID)
            }
            let finderOnscreen: [[String: Any]] = (onscreen ?? []).enumerated().compactMap { rank, row in
                guard ownerPID(row) == finderPID else { return nil }
                let identity = windowIdentity(row)
                return candidateRow(row, allRank: identity.flatMap { finderAllRanks[$0] },
                                    onscreenRank: rank, targetPID: finderPID)
            }
            if !desktopPreflightWritten {
                try appendJSON(desktopPreflightEvidence(
                    allRows: all, onscreenRows: onscreen,
                    queryStartMach: queryStartMach, queryEndMach: queryEndMach), to: output,
                    maximumFileBytes: maximumOutputBytes)
                desktopPreflightWritten = true
            }
            try appendJSON([
                "event": "pre_handoff_sample",
                "sample_index": preHandoffSampleIndex,
                "sample_started_monotonic_ns": sampleStart,
                "sample_started_mach_absolute_ns": sampleStartMach,
                "scheduled_sample_start_monotonic_ns": preHandoffNextDeadline,
                "inter_sample_gap_ns": preHandoffPreviousStart.map { sampleStart - $0 } as Any? ?? NSNull(),
                "skipped_deadlines_before_sample": preHandoffSkipped,
                "query_started_monotonic_ns": queryStart,
                "query_completed_monotonic_ns": queryEnd,
                "query_duration_ns": queryEnd - queryStart,
                "query_started_mach_absolute_ns": queryStartMach,
                "query_completed_mach_absolute_ns": queryEndMach,
                "all_window_query_started_monotonic_ns": queryStart,
                "all_window_query_started_mach_absolute_ns": queryStartMach,
                "all_window_query_completed_monotonic_ns": allEnd,
                "all_window_query_completed_mach_absolute_ns": allEndMach,
                "onscreen_window_query_started_monotonic_ns": allEnd,
                "onscreen_window_query_completed_monotonic_ns": queryEnd,
                "onscreen_window_query_started_mach_absolute_ns": allEndMach,
                "onscreen_window_query_completed_mach_absolute_ns": queryEndMach,
                "all_window_query_succeeded": all != nil,
                "onscreen_window_query_succeeded": onscreen != nil,
                "all_window_count": all?.count as Any? ?? NSNull(),
                "onscreen_window_count": onscreen?.count as Any? ?? NSNull(),
                "finder_pid": Int(finderPID),
                "display_inventory": displayRows(),
                "finder_window_count": finderAll.count,
                "finder_windows": finderAll,
                "finder_onscreen_window_count": finderOnscreen.count,
                "finder_onscreen_windows": finderOnscreen,
            ], to: output, maximumFileBytes: maximumOutputBytes)
            preHandoffSampleIndex += 1
            preHandoffPreviousStart = sampleStart
            let afterSample = DispatchTime.now().uptimeNanoseconds
            let nextDeadline = preHandoffNextDeadline &+ interval
            if nextDeadline <= afterSample {
                let skipped = (afterSample - nextDeadline) / interval + 1
                preHandoffSkipped = Int(min(skipped, UInt64(Int.max)))
                preHandoffNextDeadline = nextDeadline &+ skipped * interval
            } else {
                preHandoffSkipped = 0
                preHandoffNextDeadline = nextDeadline
            }
        }
        if let value = readJSONObject(handoffURL) { handoff = value; break }
        if FileManager.default.fileExists(atPath: completionURL.path) {
            try appendJSON(["event": "poller_result", "status": "completed_without_target_handoff",
                            "finished_at_utc": utcNow()], to: output,
                           maximumFileBytes: maximumOutputBytes)
            return 1
        }
        let untilDeadline = preHandoffNextDeadline > DispatchTime.now().uptimeNanoseconds
            ? Double(preHandoffNextDeadline - DispatchTime.now().uptimeNanoseconds) / 1_000_000_000
            : 0.02
        Thread.sleep(forTimeInterval: min(0.02, max(0.001, untilDeadline)))
    }
    guard let handoff,
          let pidNumber = handoff["target_unix_pid"] as? NSNumber,
          pidNumber.int32Value > 0,
          let runID = handoff["run_id"] as? String,
          let token = handoff["experiment_token"] as? String,
          let targetTitle = handoff["target_title"] as? String,
          !targetTitle.isEmpty else {
        try appendJSON(["event": "poller_result", "status": "invalid_or_missing_target_handoff",
                        "handoff_path": handoffURL.path, "finished_at_utc": utcNow()], to: output,
                       maximumFileBytes: maximumOutputBytes)
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
        "finder_pid": Int(finderPID),
        "handoff_path": handoffURL.path,
        "display_inventory": displayRows(),
        "poll_interval_ms": intervalMS,
        "poll_schedule": "start-to-start monotonic deadlines; missed deadlines are skipped without catch-up",
    ], to: output, maximumFileBytes: maximumOutputBytes)

    var sampleIndex = 0
    var completedDeadSamples = 0
    let captureDeadline = started + 1_500_000_000_000
    var nextScheduledSampleNS = started
    var skippedDeadlinesBeforeSample = 0
    var previousSampleStartNS: UInt64?
    while DispatchTime.now().uptimeNanoseconds < captureDeadline {
        let beforeWaitNS = DispatchTime.now().uptimeNanoseconds
        if beforeWaitNS < nextScheduledSampleNS {
            Thread.sleep(forTimeInterval: Double(nextScheduledSampleNS - beforeWaitNS) / 1_000_000_000)
        }
        if DispatchTime.now().uptimeNanoseconds >= captureDeadline { break }
        let sampleStart = DispatchTime.now().uptimeNanoseconds
        let sampleStartMach = machAbsoluteTimeNS()
        let queryStart = DispatchTime.now().uptimeNanoseconds
        let queryStartMach = machAbsoluteTimeNS()
        let allQueryStart = queryStart
        let all = rows(.optionAll)
        let allQueryEnd = DispatchTime.now().uptimeNanoseconds
        let allQueryEndMach = machAbsoluteTimeNS()
        let onscreenQueryStart = allQueryEnd
        let onscreenQueryStartMach = allQueryEndMach
        let onscreen = rows(.optionOnScreenOnly)
        let queryEnd = DispatchTime.now().uptimeNanoseconds
        let queryEndMach = machAbsoluteTimeNS()
        let coverEvidence = sameQueryCoverEvidence(
            allRows: all, onscreenRows: onscreen, targetPID: pid, targetTitle: targetTitle)
        var allRanks: [WindowIdentity: Int] = [:]
        for (rank, row) in (all ?? []).enumerated() where ownerPID(row) == pid {
            if let identity = windowIdentity(row) { allRanks[identity] = rank }
        }
        var onscreenRanks: [WindowIdentity: Int] = [:]
        for (rank, row) in (onscreen ?? []).enumerated() where ownerPID(row) == pid {
            if let identity = windowIdentity(row) { onscreenRanks[identity] = rank }
        }
        let owned: [[String: Any]] = (all ?? []).enumerated().compactMap { rank, row in
            guard ownerPID(row) == pid else { return nil }
            let identity = windowIdentity(row)
            return candidateRow(row, allRank: rank,
                                onscreenRank: identity.flatMap { onscreenRanks[$0] }, targetPID: pid)
        }
        let ownedOnscreen: [[String: Any]] = (onscreen ?? []).enumerated().compactMap { rank, row in
            guard ownerPID(row) == pid else { return nil }
            let identity = windowIdentity(row)
            return candidateRow(row, allRank: identity.flatMap { allRanks[$0] },
                                onscreenRank: rank, targetPID: pid)
        }
        var finderAllRanks: [WindowIdentity: Int] = [:]
        for (rank, row) in (all ?? []).enumerated() where ownerPID(row) == finderPID {
            if let identity = windowIdentity(row) { finderAllRanks[identity] = rank }
        }
        var finderOnscreenRanks: [WindowIdentity: Int] = [:]
        for (rank, row) in (onscreen ?? []).enumerated() where ownerPID(row) == finderPID {
            if let identity = windowIdentity(row) { finderOnscreenRanks[identity] = rank }
        }
        let finderOwned: [[String: Any]] = (all ?? []).enumerated().compactMap { rank, row in
            guard ownerPID(row) == finderPID else { return nil }
            let identity = windowIdentity(row)
            return candidateRow(row, allRank: rank,
                                onscreenRank: identity.flatMap { finderOnscreenRanks[$0] },
                                targetPID: finderPID)
        }
        let finderOwnedOnscreen: [[String: Any]] = (onscreen ?? []).enumerated().compactMap { rank, row in
            guard ownerPID(row) == finderPID else { return nil }
            let identity = windowIdentity(row)
            return candidateRow(row, allRank: identity.flatMap { finderAllRanks[$0] },
                                onscreenRank: rank, targetPID: finderPID)
        }
        try appendJSON([
            "event": "sample",
            "sample_index": sampleIndex,
            "sampled_at_utc": utcNow(),
            "sample_started_monotonic_ns": sampleStart,
            "sample_started_mach_absolute_ns": sampleStartMach,
            "scheduled_sample_start_monotonic_ns": nextScheduledSampleNS,
            "inter_sample_gap_ns": previousSampleStartNS.map { sampleStart - $0 } as Any? ?? NSNull(),
            "skipped_deadlines_before_sample": skippedDeadlinesBeforeSample,
            "query_started_monotonic_ns": queryStart,
            "query_completed_monotonic_ns": queryEnd,
            "query_duration_ns": queryEnd - queryStart,
            "query_started_mach_absolute_ns": queryStartMach,
            "query_completed_mach_absolute_ns": queryEndMach,
            "all_window_query_started_monotonic_ns": allQueryStart,
            "all_window_query_completed_monotonic_ns": allQueryEnd,
            "all_window_query_started_mach_absolute_ns": queryStartMach,
            "all_window_query_completed_mach_absolute_ns": allQueryEndMach,
            "onscreen_query_started_monotonic_ns": onscreenQueryStart,
            "onscreen_query_completed_monotonic_ns": queryEnd,
            "onscreen_query_started_mach_absolute_ns": onscreenQueryStartMach,
            "onscreen_query_completed_mach_absolute_ns": queryEndMach,
            "all_window_query_succeeded": all != nil,
            "onscreen_window_query_succeeded": onscreen != nil,
            "all_window_count": all?.count as Any? ?? NSNull(),
            "onscreen_window_count": onscreen?.count as Any? ?? NSNull(),
            "target_unix_pid": Int(pid),
            "run_id": runID,
            "experiment_token": token,
            "same_pid_window_count": owned.count,
            "windows": owned,
            "same_pid_onscreen_window_count": ownedOnscreen.count,
            "onscreen_windows": ownedOnscreen,
            "finder_pid": Int(finderPID),
            "finder_window_count": finderOwned.count,
            "finder_windows": finderOwned,
            "finder_onscreen_window_count": finderOwnedOnscreen.count,
            "finder_onscreen_windows": finderOwnedOnscreen,
            "same_query_cover_evidence": coverEvidence,
        ], to: output, maximumFileBytes: maximumOutputBytes)
        sampleIndex += 1
        previousSampleStartNS = sampleStart

        let completionMatches = matchingCompletion(completionURL, runID: runID,
                                                   token: token, pid: pid)
        if completionMatches && !processIsAlive(pid) {
            completedDeadSamples += 1
            if completedDeadSamples >= 3 { break }
        } else {
            completedDeadSamples = 0
        }
        let nextDeadline = nextScheduledSampleNS &+ interval
        let afterSampleNS = DispatchTime.now().uptimeNanoseconds
        if nextDeadline <= afterSampleNS {
            let skipped = (afterSampleNS - nextDeadline) / interval + 1
            skippedDeadlinesBeforeSample = Int(min(skipped, UInt64(Int.max)))
            nextScheduledSampleNS = nextDeadline &+ skipped * interval
        } else {
            skippedDeadlinesBeforeSample = 0
            nextScheduledSampleNS = nextDeadline
        }
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
    ], to: output, maximumFileBytes: maximumOutputBytes)
    return completionObserved && !processIsAlive(pid) ? 0 : 1
}

exit(try run())
