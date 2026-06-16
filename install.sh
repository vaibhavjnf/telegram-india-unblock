#!/usr/bin/env bash
# install.sh — one-shot installer for the Telegram India Unblock hotfix.
#
# Makes your Hermes gateway's Telegram channel survive an ISP block of
# api.telegram.org by routing the Bot API through a live SOCKS5 exit, and
# keeps it healed automatically via a launchd timer (macOS) or cron (Linux).
#
#   curl -fsSL <raw>/install.sh | bash      # or: bash install.sh
#
# Idempotent. Safe to re-run. Uninstall with: bash install.sh --uninstall
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/vaibhavjnf/telegram-india-unblock/main}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="${TGUNBLOCK_DIR:-$HOME/.hermes/telegram-india-unblock}"
LABEL="ai.hermes.telegram-proxy-failover"
GATEWAY_LABEL="${TGFAILOVER_GATEWAY_LABEL:-ai.hermes.gateway}"
PY="$(command -v python3 || true)"

c_grn=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say() { echo "${c_grn}[tg-unblock]${c_rst} $*"; }
warn(){ echo "${c_red}[tg-unblock]${c_rst} $*" >&2; }

# ---------------------------------------------------------------- uninstall
if [ "${1:-}" = "--uninstall" ]; then
  say "Uninstalling..."
  if [[ "$OSTYPE" == darwin* ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
  else
    ( crontab -l 2>/dev/null | grep -v "telegram_proxy_failover.py" ) | crontab - 2>/dev/null || true
  fi
  # remove the proxy line so the gateway goes direct again
  if [ -f "$HERMES_HOME/.env" ]; then
    "$PY" - "$HERMES_HOME/.env" <<'PYEOF' || true
import re,sys,pathlib
p=pathlib.Path(sys.argv[1]); t=p.read_text()
t="\n".join(l for l in t.splitlines()
           if not l.startswith("TELEGRAM_PROXY=") and "# telegram-india-unblock" not in l)
p.write_text(t+("\n" if not t.endswith("\n") else ""))
PYEOF
  fi
  say "Removed timer + TELEGRAM_PROXY line. Restart your gateway to go direct:"
  echo "    launchctl kickstart -k gui/$(id -u)/$GATEWAY_LABEL   ${c_dim}# macOS${c_rst}"
  exit 0
fi

# --------------------------------------------------------------- preflight
[ -n "$PY" ] || { warn "python3 not found on PATH"; exit 1; }
command -v curl >/dev/null || { warn "curl not found on PATH"; exit 1; }
[ -d "$HERMES_HOME" ] || { warn "HERMES_HOME ($HERMES_HOME) not found. Set HERMES_HOME and re-run."; exit 1; }
mkdir -p "$DEST" "$HERMES_HOME/logs"

# ---------------------------------------------- fetch script + pool + plist
fetch() { # fetch <relpath> <dest>
  if [ -f "$(dirname "${BASH_SOURCE[0]}")/$1" ]; then
    cp "$(dirname "${BASH_SOURCE[0]}")/$1" "$2"          # local run
  else
    curl -fsSL "$REPO_RAW/$1" -o "$2"                     # piped run
  fi
}
say "Installing into $DEST"
fetch telegram_proxy_failover.py "$DEST/telegram_proxy_failover.py"
[ -f "$DEST/proxy_pool.txt" ] || fetch proxy_pool.txt "$DEST/proxy_pool.txt"  # keep user's pool edits
chmod +x "$DEST/telegram_proxy_failover.py"

# ------------------------------------------------------ first heal (now!)
say "Running first heal..."
HERMES_HOME="$HERMES_HOME" TGFAILOVER_GATEWAY_LABEL="$GATEWAY_LABEL" \
  TGFAILOVER_POOL_FILE="$DEST/proxy_pool.txt" \
  "$PY" "$DEST/telegram_proxy_failover.py" || warn "first heal returned non-zero (no live exit yet?)"

# ---------------------------------------------------- persist the timer
if [[ "$OSTYPE" == darwin* ]]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  TMP="$(mktemp)"
  fetch ai.hermes.telegram-proxy-failover.plist.template "$TMP"
  sed -e "s#__PYTHON__#$PY#g" \
      -e "s#__SCRIPT__#$DEST/telegram_proxy_failover.py#g" \
      -e "s#__HERMES_HOME__#$HERMES_HOME#g" \
      "$TMP" > "$PLIST"
  rm -f "$TMP"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null || true
  say "launchd timer installed ($LABEL, every 5 min, runs at login)."
else
  # Linux: cron every 5 min
  LINE="*/5 * * * * HERMES_HOME=$HERMES_HOME TGFAILOVER_GATEWAY_LABEL=$GATEWAY_LABEL TGFAILOVER_POOL_FILE=$DEST/proxy_pool.txt $PY $DEST/telegram_proxy_failover.py >> $HERMES_HOME/logs/telegram-proxy-failover.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v "telegram_proxy_failover.py"; echo "$LINE" ) | crontab -
  say "cron timer installed (every 5 min)."
fi

echo
say "${c_grn}Done.${c_rst} Telegram will stay alive through the ISP block and self-heal."
echo "  status : $PY $DEST/telegram_proxy_failover.py --status"
echo "  logs   : tail -f $HERMES_HOME/logs/telegram-proxy-failover.log"
echo "  remove : bash install.sh --uninstall"
