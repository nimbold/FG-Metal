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

guard CommandLine.arguments.count == 7,
      let targetPID = Int(CommandLine.arguments[1]),
      let targetWindow = Int(CommandLine.arguments[2]),
      let covererPID = Int(CommandLine.arguments[3]),
      let covererWindow = Int(CommandLine.arguments[4]),
      let durationMS = Int(CommandLine.arguments[5]) else {
    fputs("usage: public-cg-poller <target-pid> <target-window> <coverer-pid> <coverer-window> <duration-ms> <output-jsonl>\n", stderr)
    exit(64)
}
let outputURL = URL(fileURLWithPath: CommandLine.arguments[6])
FileManager.default.createFile(atPath: outputURL.path, contents: nil)
guard let output = try? FileHandle(forWritingTo: outputURL) else { exit(73) }
defer { try? output.close() }

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
    let covererPair = onScreen.enumerated().first { matches($0.element, pid: covererPID, window: covererWindow) }
    let allTarget = all.first { matches($0, pid: targetPID, window: targetWindow) }
    let allCoverer = all.first { matches($0, pid: covererPID, window: covererWindow) }
    let targetBounds = targetPair.flatMap { rect($0.element[kCGWindowBounds as String]) }
    let covererBounds = covererPair.flatMap { rect($0.element[kCGWindowBounds as String]) }
    let intersection = targetBounds.flatMap { target in covererBounds.map { $0.intersection(target) } }
    let coverage: Double
    if let targetBounds, let intersection, !intersection.isNull,
       targetBounds.width > 0, targetBounds.height > 0 {
        coverage = intersection.width * intersection.height / (targetBounds.width * targetBounds.height)
    } else { coverage = 0 }
    let covererRow: [String: Any]? = covererPair?.element
    let targetRow: [String: Any]? = targetPair?.element
    let targetLayer = (targetRow?[kCGWindowLayer as String] as? NSNumber)?.intValue
    let covererLayer = (covererRow?[kCGWindowLayer as String] as? NSNumber)?.intValue
    try writeLine([
        "sampleIndex": index,
        "sampledAtMonotonicNS": sampleStart,
        "elapsedSincePollerStartNS": sampleStart - start,
        "querySucceeded": true,
        "onScreenWindowCount": onScreen.count,
        "allWindowCount": all.count,
        "targetOnScreen": targetPair != nil,
        "covererOnScreen": covererPair != nil,
        "target": rowJSON(targetRow, rank: targetPair?.offset),
        "coverer": rowJSON(covererRow, rank: covererPair?.offset),
        "targetAllWindows": rowJSON(allTarget, rank: nil),
        "covererAllWindows": rowJSON(allCoverer, rank: nil),
        "targetAreaCoverageFraction": coverage,
        "covererInFrontOfTarget": covererPair != nil && targetPair != nil && covererPair!.offset < targetPair!.offset,
        "covererLayerAboveTarget": covererLayer != nil && targetLayer != nil && covererLayer! > targetLayer!,
    ], to: output)
    index += 1
    let elapsed = DispatchTime.now().uptimeNanoseconds - sampleStart
    if elapsed < interval { Thread.sleep(forTimeInterval: Double(interval - elapsed) / 1_000_000_000) }
}
try output.synchronize()
