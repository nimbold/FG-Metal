#!/bin/sh

# Shared checks for helpers that operate on local DXMT copies.
dxmt_canonical_path() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

dxmt_is_temporary_path() {
  case "$1" in
    "$2"/*|/tmp/*|/private/tmp/*|/var/folders/*/T/*) return 0 ;;
    *) return 1 ;;
  esac
}

dxmt_is_within_root() {
  case "$1" in
    "$2"/*) return 0 ;;
    *) return 1 ;;
  esac
}

dxmt_is_installed_engine_path() {
  case "$1" in
    /Applications/*|*/Highball.app/*|*/Library/Application\ Support/Highball/engines/*)
      return 0
      ;;
    *) return 1 ;;
  esac
}
