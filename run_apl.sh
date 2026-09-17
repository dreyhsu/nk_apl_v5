#!/bin/sh
set -eu

APP_DIR=${APL_APP_DIR:-/var/rootdirs/opt/VisionEdge_APL}
cd "$APP_DIR"
exec /usr/bin/python3 -B -u visionedge_server.py
