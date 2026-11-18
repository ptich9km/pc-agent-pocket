#!/usr/bin/env bash
# Installer for the Telegram <-> OpenCode bridge (Linux).
#
# What it does:
#   1. Checks prerequisites: python3 >= 3.8, opencode binary.
#   2. Installs bridge.py into ~/.config/tg-opencode/ (runtime layout).
#   3. Creates the bot token file if missing (never overwrites an existing one).
#   4. Installs and enables the systemd user service tg-opencode.service.
#   5. Prints the pairing code to send to the bot (six digits from the log).
#
# Usage:
#   ./setup.sh                 interactive menu when run in a terminal without
#                              arguments (install / check / status / restart /
#                              uninstall); plain install otherwise
#   ./setup.sh -y              non-interactive (accept defaults)
#   ./setup.sh --check         only run prerequisite checks, change nothing
#   ./setup.sh --skip-token-check   do not validate the token against Telegram
#   ./setup.sh --uninstall     stop/remove the service and installed script
#   ./setup.sh --uninstall --purge  also delete token, state and log
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG_DIR="$HOME/.config/tg-opencode"
TOKEN_DIR="$CONFIG_DIR/token"
TOKEN_FILE="$TOKEN_DIR/token"
STATE_FILE="$CONFIG_DIR/state.json"
LOG_FILE="$CONFIG_DIR/bridge.log"
UNIT_NAME="tg-opencode.service"
UNIT_DIR="$HOME/.config/systemd/user"
OPENCODE_BIN="$HOME/.opencode/bin/opencode"

ASSUME_YES=0
CHECK_ONLY=0
SKIP_TOKEN_CHECK=0
UNINSTALL=0
PURGE=0
STARTED=0

usage() {
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
}

ARGC=$#

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --check) CHECK_ONLY=1 ;;
        --skip-token-check) SKIP_TOKEN_CHECK=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

ask() {
    # ask "question" -> returns 0 for yes (default), 1 for no
    if [[ $ASSUME_YES -eq 1 ]]; then
        return 0
    fi
    local answer
    read -r -p "$1 [Y/n] " answer
    [[ -z $answer || $answer =~ ^[Yy] ]]
}

unit_managed() {
    # True only if the user manager loaded this unit name from the exact unit
    # file path this script manages. systemctl --user talks to the real user
    # manager regardless of $HOME, so this guard prevents touching a foreign
    # or same-named unit installed elsewhere.
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1 || return 1
    local fragment
    fragment=$(systemctl --user show -p FragmentPath --value "$UNIT_NAME" 2>/dev/null || true)
    [[ -n $fragment && "$fragment" == "$UNIT_DIR/$UNIT_NAME" ]]
}

user_systemd_available() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1
}

show_status() {
    if ! user_systemd_available; then
        warn "no usable systemd user session; the service cannot be inspected"
        say "run the bridge in the foreground with: $SCRIPT_DIR/run.sh"
        return 0
    fi
    if unit_managed; then
        systemctl --user status "$UNIT_NAME" --no-pager || true
    else
        warn "service $UNIT_NAME is not installed from $UNIT_DIR"
    fi
    say ""
    say "Last log lines ($UNIT_NAME):"
    journalctl --user -u "$UNIT_NAME" -n 20 --no-pager 2>/dev/null || say "(no log yet)"
}

restart_service() {
    if ! user_systemd_available; then
        warn "no usable systemd user session; nothing to restart"
        say "run the bridge in the foreground with: $SCRIPT_DIR/run.sh"
        return 0
    fi
    if ! unit_managed; then
        warn "service $UNIT_NAME is not installed from $UNIT_DIR"
        say "choose option 1 to install it first"
        return 0
    fi
    if systemctl --user is-active --quiet "$UNIT_NAME"; then
        if systemctl --user restart "$UNIT_NAME"; then
            say "ok: service restarted"
        else
            warn "service restart failed; see: journalctl --user -u $UNIT_NAME -n 50"
        fi
    else
        warn "service $UNIT_NAME is installed but not running"
        say "start it with: systemctl --user start $UNIT_NAME"
    fi
}

pairing_code() {
    # Six-digit one-time code the bridge logs on start while no chat is bound.
    [[ -f $LOG_FILE ]] || return 1
    sed -n 's/.*pairing required, code: \([0-9][0-9]*\).*/\1/p' "$LOG_FILE" | tail -n 1
}

bot_username() {
    # "@name" of the bot, as logged by the bridge after a successful start.
    [[ -f $LOG_FILE ]] || return 1
    sed -n 's/.*bridge started as \(@[A-Za-z0-9_]*\).*/\1/p' "$LOG_FILE" | tail -n 1
}

chat_bound() {
    # True when state.json already holds a chat id (bridge already paired).
    [[ -s $STATE_FILE ]] || return 1
    "$PYTHON_BIN" - "$STATE_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        state = json.load(handle)
except Exception:
    sys.exit(1)
sys.exit(0 if state.get("chat_id") else 1)
PY
}

started_lines() {
    # Number of "bridge started" lines in the log (0 when the log is absent).
    local count=""
    if [[ -f $LOG_FILE ]]; then
        count=$(grep -c 'bridge started as' "$LOG_FILE" 2>/dev/null || true)
    fi
    printf '%s\n' "${count:-0}"
}

wait_for_start_line() {
    # $1: startup-line count before the service was started; waits until the
    # log gains a new startup line so the printed bot name is the fresh one.
    local previous=${1:-0} i count
    for ((i = 0; i < 20; i++)); do
        count=$(started_lines)
        if [[ ${count:-0} -gt $previous ]]; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

# ── interactive menu ─────────────────────────────────────────────────────────

# Shown only when started with no arguments from a terminal. Every option
# either sets the same flags as the CLI options and falls through to the
# existing linear flow, or performs a read-only/immediate action and returns
# to the menu.
if [[ $ARGC -eq 0 && -t 0 && -t 1 ]]; then
    while :; do
        say ""
        say "Telegram <-> OpenCode bridge installer"
        say "  1) Install or update the bridge"
        say "  2) Check prerequisites only (changes nothing)"
        say "  3) Show service status and last log lines"
        say "  4) Restart the service"
        say "  5) Uninstall: stop service and remove script (keep token, state, log)"
        say "  6) Uninstall and delete everything (token, state, log)"
        say "  0) Exit"
        read -r -p "Choose an option [0-6]: " choice || exit 0
        case "$choice" in
            1) say ""; break ;;
            2) CHECK_ONLY=1; say ""; break ;;
            3) show_status ;;
            4) restart_service ;;
            5) UNINSTALL=1; say ""; break ;;
            6) UNINSTALL=1; PURGE=1; say ""; break ;;
            0|"") say "Nothing changed."; exit 0 ;;
            *) warn "unknown option: $choice" ;;
        esac
    done
fi

# ── uninstall mode ───────────────────────────────────────────────────────────

if [[ $UNINSTALL -eq 1 ]]; then
    say "== Uninstalling =="
    if unit_managed; then
        systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
        rm -f "$UNIT_DIR/$UNIT_NAME"
        systemctl --user daemon-reload
        say "ok: service stopped and unit removed"
    else
        rm -f "$UNIT_DIR/$UNIT_NAME"
    fi
    rm -f "$CONFIG_DIR/bridge.py" "$CONFIG_DIR/bridge.py.bak"
    say "ok: installed script removed"
    if [[ $PURGE -eq 1 ]]; then
        if ask "Also delete token, state and log ($CONFIG_DIR)?"; then
            rm -rf "$CONFIG_DIR"
            say "ok: config directory removed"
        fi
    else
        say "kept: token/state/log in $CONFIG_DIR (use --purge to remove them)"
    fi
    exit 0
fi

# ── 1. prerequisite checks ───────────────────────────────────────────────────

say "== Checking prerequisites =="

[[ -f "$SCRIPT_DIR/bridge.py" ]] || die "bridge.py not found next to setup.sh ($SCRIPT_DIR)"
say "ok: bridge.py found"

command -v python3 >/dev/null 2>&1 || die "python3 is not installed (install it with your package manager)"
PYTHON_BIN=$(command -v python3)
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    die "python3 >= 3.8 is required (found: $("$PYTHON_BIN" --version 2>&1))"
fi
say "ok: $("$PYTHON_BIN" --version 2>&1) ($PYTHON_BIN)"

if [[ -x "$OPENCODE_BIN" ]]; then
    say "ok: opencode binary at $OPENCODE_BIN"
else
    warn "opencode binary not found at $OPENCODE_BIN"
    warn "the bridge will not be able to talk to OpenCode until it is installed there"
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    say "== Check-only mode: nothing was changed =="
    exit 0
fi

# ── 2. install the script ────────────────────────────────────────────────────

say "== Installing bridge.py =="
mkdir -p "$CONFIG_DIR" "$TOKEN_DIR"
chmod 700 "$CONFIG_DIR" "$TOKEN_DIR"

if [[ -f "$CONFIG_DIR/bridge.py" ]] && ! cmp -s "$SCRIPT_DIR/bridge.py" "$CONFIG_DIR/bridge.py"; then
    cp -p "$CONFIG_DIR/bridge.py" "$CONFIG_DIR/bridge.py.bak"
    say "previous copy saved as $CONFIG_DIR/bridge.py.bak"
fi
install -m 755 "$SCRIPT_DIR/bridge.py" "$CONFIG_DIR/bridge.py"
say "ok: installed $CONFIG_DIR/bridge.py"

# ── 3. bot token ─────────────────────────────────────────────────────────────

say "== Bot token =="
if [[ -s "$TOKEN_FILE" ]]; then
    say "ok: existing token kept ($TOKEN_FILE)"
else
    [[ -t 0 ]] || die "no token found and stdin is not a terminal; create $TOKEN_FILE manually"
    read -r -s -p "Paste the Telegram bot token (from @BotFather): " token
    echo
    [[ -n $token ]] || die "empty token"
    if [[ $SKIP_TOKEN_CHECK -eq 0 ]]; then
        if "$PYTHON_BIN" - "$token" <<'PY'
import json, sys, urllib.request
token = sys.argv[1]
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/getMe", timeout=15
    ) as response:
        result = json.loads(response.read().decode())
except Exception as error:
    print(f"WARNING: could not reach Telegram API ({error}); skipping validation", file=sys.stderr)
    sys.exit(0)
if result.get("ok"):
    print(f"ok: token valid, bot @{result['result'].get('username')}")
    sys.exit(0)
print(f"ERROR: Telegram rejected the token: {result.get('description')}", file=sys.stderr)
sys.exit(1)
PY
        then :; else die "token validation failed (use --skip-token-check to bypass)"; fi
    fi
    printf '%s\n' "$token" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    unset token
    say "ok: token saved to $TOKEN_FILE"
fi

# ── 4. systemd user service ──────────────────────────────────────────────────

say "== systemd user service =="
START_LINES=$(started_lines)
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=Telegram bridge for OpenCode
After=default.target

[Service]
Type=simple
WorkingDirectory=%h
ExecStart=/usr/bin/systemd-inhibit --what=idle:sleep --why=opencode-telegram-bridge --mode=block $PYTHON_BIN %h/.config/tg-opencode/bridge.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
say "ok: wrote $UNIT_DIR/$UNIT_NAME"

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    if unit_managed; then
        if systemctl --user is-active --quiet "$UNIT_NAME"; then
            if ask "Service is already running. Restart it to load the installed copy?"; then
                systemctl --user restart "$UNIT_NAME"
                STARTED=1
                say "ok: service restarted"
            else
                say "service left running with the old copy; restart later with: systemctl --user restart $UNIT_NAME"
            fi
        else
            if ask "Enable and start $UNIT_NAME now?"; then
                systemctl --user enable --now "$UNIT_NAME"
                STARTED=1
                say "ok: service enabled and started"
            fi
        fi
    else
        warn "unit $UNIT_NAME is not loaded from $UNIT_DIR; service state left untouched"
        say "start manually with: $SCRIPT_DIR/run.sh"
    fi
    say ""
    say "Useful commands:"
    say "  systemctl --user status $UNIT_NAME"
    say "  journalctl --user -u $UNIT_NAME -f"
else
    warn "no usable systemd user session detected; service file installed but not started"
    say "start manually with: $SCRIPT_DIR/run.sh"
    say "for autostart without a login session: loginctl enable-linger $USER"
fi

say ""
say "== Done =="

if [[ $STARTED -eq 1 ]]; then
    wait_for_start_line "$START_LINES" || true
fi
bot=$(bot_username || true)
code=$(pairing_code || true)

if chat_bound; then
    say "ok: the bot is already bound to a chat; nothing to pair."
    say "The bridge serves only that chat, every other chat is ignored."
elif [[ -n $code ]]; then
    say "Next step — pair the bridge with your Telegram chat:"
    say ""
    if [[ -n $bot ]]; then
        say "  1. Open the bot $bot in Telegram"
    else
        say "  1. Open your bot in Telegram (its @name is in the log below)"
    fi
    say "  2. Send it exactly this message:  /pair $code"
    say ""
    say "The code is a one-time password the bridge prints on start; it stays"
    say "valid across restarts and changes only when state.json is deleted"
    say "(--uninstall --purge). All chats except the paired one are ignored."
else
    say "The bridge has not written a pairing code yet (it may still be starting)."
    say "Read it in a few seconds with:"
    say "  journalctl --user -u $UNIT_NAME -n 100 --no-pager | grep 'pairing required'"
    say "or directly from the log file:"
    say "  grep 'pairing required' $LOG_FILE"
    say "Then send '/pair <code>' to the bot from the Telegram chat you want to bind."
fi
