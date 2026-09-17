#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec python3 -u visionedge_server.py "$@"
