import CoreGraphics
import Darwin
import Foundation

func rect(_ value: Any?) -> CGRect? {
    guard let dictionary = value as? NSDictionary else { return nil }
    var result = CGRect.zero
    return CGRectMakeWithDictionaryRepresentation(dictionary as CFDictionary, &result) ? result : nil
}

func rectJSON(_ value: CGRect?) -> Any {
    guard let value else { return NSNull() }
    return ["x": value.origin.x, "y": value.origin.y,
            "width": value.width, "height": value.height,
            "minX": value.minX, "minY": value.minY,
            "maxX": value.maxX, "maxY": value.maxY]
}

func rowJSON(_ row: [String: Any]?, rank: Int?) -> Any {
    guard let row else { return NSNull() }
    return [
        "ownerPID": row[kCGWindowOwnerPID as String] ?? NSNull(),
        "windowNumber": row[kCGWindowNumber as String] ?? NSNull(),
        "ownerName": row[kCGWindowOwnerName as String] ?? NSNull(),
        "title": row[kCGWindowName as String] ?? NSNull(),
        "isOnScreen": row[kCGWindowIsOnscreen as String] ?? NSNull(),
        "bounds": rectJSON(rect(row[kCGWindowBounds as String])),
        "layer": row[kCGWindowLayer as String] ?? NSNull(),
        "alpha": row[kCGWindowAlpha as String] ?? NSNull(),
        "rankFrontToBack": rank.map { $0 } as Any? ?? NSNull(),
    ]
}

func matches(_ row: [String: Any], pid: Int, window: Int) -> Bool {
    (row[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == pid
        && (row[kCGWindowNumber as String] as? NSNumber)?.intValue == window
}

func writeLine(_ value: [String: Any], to handle: FileHandle) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    try handle.write(contentsOf: data)
    try handle.write(contentsOf: Data([0x0A]))
}

guard CommandLine.arguments.count == 6,
      let targetPID = Int(CommandLine.arguments[1]),
      let targetWindow = Int(CommandLine.arguments[2]),
      let finderPID = Int(CommandLine.arguments[3]),
      let durationMS = Int(CommandLine.arguments[4]) else {
    fputs("usage: finder-window-poller <target-pid> <target-window> <finder-pid> <duration-ms> <output-jsonl>\n", stderr)
    exit(64)
}
let outputURL = URL(fileURLWithPath: CommandLine.arguments[5])
guard !FileManager.default.fileExists(atPath: outputURL.path),
      FileManager.default.createFile(atPath: outputURL.path, contents: nil),
      let output = try? FileHandle(forWritingTo: outputURL) else { exit(73) }
defer { try? output.close() }

let expectedBounds = CGRect(x: 68, y: 62, width: 640, height: 392)
let start = DispatchTime.now().uptimeNanoseconds
let deadline = start + UInt64(durationMS) * 1_000_000
let interval: UInt64 = 10_000_000
var index = 0
while DispatchTime.now().uptimeNanoseconds < deadline {
    let sampleStart = DispatchTime.now().uptimeNanoseconds
    guard let onScreen = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]],
          let all = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else {
        try? writeLine(["sampleIndex": index, "sampledAtMonotonicNS": sampleStart,
                        "querySucceeded": false], to: output)
        index += 1
        Thread.sleep(forTimeInterval: 0.01)
        continue
    }
    let targetPair = onScreen.enumerated().first { matches($0.element, pid: targetPID, window: targetWindow) }
    let allTarget = all.first { matches($0, pid: targetPID, window: targetWindow) }
    let targetBounds = targetPair.flatMap { rect($0.element[kCGWindowBounds as String]) }
    let finderPairs = onScreen.enumerated().filter {
        ($0.element[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == finderPID
    }
    let targetLayer = targetPair.flatMap { ($0.element[kCGWindowLayer as String] as? NSNumber)?.intValue }
    let candidates = finderPairs.map { pair -> [String: Any] in
        let bounds = rect(pair.element[kCGWindowBounds as String])
        let alpha = (pair.element[kCGWindowAlpha as String] as? NSNumber)?.doubleValue
        let isExact = bounds.map {
            abs($0.minX - expectedBounds.minX) < 0.5
                && abs($0.minY - expectedBounds.minY) < 0.5
                && abs($0.width - expectedBounds.width) < 0.5
                && abs($0.height - expectedBounds.height) < 0.5
        } ?? false
        let fullyContains = bounds.flatMap { cover in
            targetBounds.map { target in cover.contains(target) }
        } ?? false
        let ahead = targetPair.map { pair.offset < $0.offset } ?? false
        let layer = (pair.element[kCGWindowLayer as String] as? NSNumber)?.intValue
        let alphaOne = alpha.map { abs($0 - 1.0) < 0.0001 } ?? false
        let qualifies = targetPair != nil && isExact && fullyContains && ahead && alphaOne
        return [
            "window": rowJSON(pair.element, rank: pair.offset),
            "exactExpectedBounds": isExact,
            "fullyContainsTarget": fullyContains,
            "aheadOfTarget": ahead,
            "alphaExactlyOne": alphaOne,
            "layerAboveTarget": layer != nil && targetLayer != nil && layer! > targetLayer!,
            "qualifiesFullCover": qualifies,
        ]
    }
    let qualifying = candidates.first {
        ($0["qualifiesFullCover"] as? Bool) == true
    }
    try writeLine([
        "sampleIndex": index,
        "sampledAtMonotonicNS": sampleStart,
        "elapsedSincePollerStartNS": sampleStart - start,
        "querySucceeded": true,
        "onScreenWindowCount": onScreen.count,
        "allWindowCount": all.count,
        "targetOnScreen": targetPair != nil,
        "target": rowJSON(targetPair?.element, rank: targetPair?.offset),
        "targetAllWindows": rowJSON(allTarget, rank: nil),
        "finderCandidateCount": candidates.count,
        "finderCandidates": candidates,
        "finderFullCover": qualifying != nil,
        "qualifyingFinderWindow": qualifying?["window"] ?? NSNull(),
        "targetAreaCoverageFraction": qualifying == nil ? 0.0 : 1.0,
    ], to: output)
    index += 1
    let elapsed = DispatchTime.now().uptimeNanoseconds - sampleStart
    if elapsed < interval { Thread.sleep(forTimeInterval: Double(interval - elapsed) / 1_000_000_000) }
}
try output.synchronize()
