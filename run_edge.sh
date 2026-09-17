#!/bin/sh
# Backward-compatible launcher. VisionEdge is the project/product name.
set -eu
cd "$(dirname "$0")"
exec python3 -u visionedge_server.py "$@"
