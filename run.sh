#!/usr/bin/env bash
# Run the bridge in the foreground (debug/manual mode).
# NOTE: only one bridge instance may poll the bot at a time. If the systemd
# user service tg-opencode.service is active, stop it first:
#   systemctl --user stop tg-opencode.service
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if command -v systemctl >/dev/null 2>&1 \
    && systemctl --user is-active --quiet tg-opencode.service 2>/dev/null; then
    echo "ERROR: tg-opencode.service is already running; stop it first to avoid double polling." >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/bridge.py"
