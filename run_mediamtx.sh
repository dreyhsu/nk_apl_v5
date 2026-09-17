#!/bin/sh
set -eu

APP_DIR=${APL_APP_DIR:-/var/rootdirs/opt/VisionEdge_APL}
MEDIAMTX_BIN=${APL_MEDIAMTX_BIN:-/var/rootdirs/opt/mediamtx/mediamtx}
exec "$MEDIAMTX_BIN" "$APP_DIR/mediamtx.yml"
