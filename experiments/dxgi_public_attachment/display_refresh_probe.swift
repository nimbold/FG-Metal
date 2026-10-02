import CoreGraphics
import Darwin

var displayCount: UInt32 = 0
guard CGGetActiveDisplayList(0, nil, &displayCount) == .success else {
    fputs("CGGetActiveDisplayList(count) failed\n", stderr)
    exit(1)
}

var displays = [CGDirectDisplayID](repeating: 0, count: Int(displayCount))
guard CGGetActiveDisplayList(displayCount, &displays, &displayCount) == .success else {
    fputs("CGGetActiveDisplayList(ids) failed\n", stderr)
    exit(1)
}

for display in displays {
    guard let mode = CGDisplayCopyDisplayMode(display) else {
        print("display_id=\(display) mode=unavailable")
        continue
    }
    print("display_id=\(display) width=\(mode.pixelWidth) height=\(mode.pixelHeight) " +
          "refresh_hz=\(mode.refreshRate)")
}
