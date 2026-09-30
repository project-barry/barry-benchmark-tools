#!/bin/sh
# Copy the harness to the device's ~/bench (results/, opt/ and state/ there are left alone).
#   scripts/deploy.sh [user@host]      default: $BBT_TARGET
set -eu
target="${1:-${BBT_TARGET:?set BBT_TARGET=user@host or pass it}}"
cd "$(dirname "$0")/.."
# COPYFILE_DISABLE: macOS tar would otherwise add AppleDouble ._* files
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -cf - --exclude '__pycache__' --exclude '.git' --exclude 'results' \
    bench bbt bin matrices scripts README.md 2>/dev/null |
  ssh ${BBT_SSH_OPTS:-} "$target" 'mkdir -p ~/bench && tar -xf - -C ~/bench && chmod +x ~/bench/bench ~/bench/bin/* && echo "deployed to $(hostname):~/bench"'
