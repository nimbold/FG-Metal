#!/bin/sh
# Restore the Highball bottle configs to their pre-R114 state.
# Usage: sh restore_bottles.sh
set -eu
BOTTLES="$HOME/Library/Application Support/Highball/bottles"
cp /tmp/bottle.json.r114-backup "$BOTTLES/Games/bottle.json"
cp /tmp/bottle.json.play-r114-backup "$BOTTLES/play/bottle.json"
rm -f "$BOTTLES/play/drive_c/windows/system32/d3d12_d3dmetal.dll"
rm -f "$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r28/renderers/d3dmetal-tsshim/wine/x86_64-windows/d3d12_d3dmetal.dll"
echo "restored: Games and play bottle.json, stray d3d12_d3dmetal.dll removed"
