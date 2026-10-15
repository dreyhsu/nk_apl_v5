#!/bin/sh
set -eu

APP_DIR=${APL_APP_DIR:-/var/rootdirs/opt/VisionEdge_APL}
case "${VISIONEDGE_TLS:-on}" in
    on|1|true|yes) ;;
    *) echo "Production APL requires HTTPS (VISIONEDGE_TLS=on)" >&2; exit 2 ;;
esac
if [ "${APL_DMZ_MODE:-http}" != http ]; then
    echo "Production APL requires APL_DMZ_MODE=http" >&2
    exit 2
fi
case "${APL_DMZ_BYPASS:-off}" in
    off|0|false|no) ;;
    *) echo "Production APL cannot bypass SFIS" >&2; exit 2 ;;
esac
cd "$APP_DIR"
exec /usr/bin/python3 -B -u visionedge_server.py
