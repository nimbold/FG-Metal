#!/bin/sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
/usr/bin/clang -O2 -fobjc-arc \
  -isysroot /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk \
  -mmacosx-version-min=14.0 \
  -framework Foundation -framework Metal \
  "$HERE/r106_gpu_motion_probe.m" -o "$HERE/r106_gpu_motion_probe"
