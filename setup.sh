#!/usr/bin/env bash
# setup.sh — one friendly command to keep your Telegram bot alive through an
# ISP block. Walks you through it, picks the best option, wires it up, and
# verifies it actually works. No jargon, no guesswork.
#
#   bash setup.sh
#
# Non-interactive (CI / scripted):
#   CF_API_TOKEN=xxx CF_ACCOUNT_ID=xxx bash setup.sh --worker --yes
#   bash setup.sh --proxy --yes        # SOCKS5 self-healer only, no Cloudflare
set -uo pipefail

# ---------- pretty ----------
if [ -t 1 ]; then
  B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; X=$'\033[0m'
else B=""; D=""; G=""; Y=""; R=""; C=""; X=""; fi
ok()   { echo "${G}✓${X} $*"; }
warn() { echo "${Y}!${X} $*"; }
err()  { echo "${R}✗${X} $*" >&2; }
step() { echo; echo "${B}${C}▸ $*${X}"; }
ask()  { local p="$1" d="${2:-}" r; if [ "$AUTO" = 1 ]; then echo "$d"; return; fi
         read -r -p "$(printf '%s ' "$p")" r </dev/tty; echo "${r:-$d}"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
GATEWAY_LABEL="${TGFAILOVER_GATEWAY_LABEL:-ai.hermes.gateway}"
WORKER_NAME="${WORKER_NAME:-tg-proxy}"
AUTO=0; MODE=""
for a in "$@"; do case "$a" in
  --yes|-y) AUTO=1;; --worker) MODE=worker;; --proxy) MODE=proxy;;
  -h|--help) sed -n '2,11p' "$0"; exit 0;; esac; done

bot_token() { # read TELEGRAM_BOT_TOKEN from ~/.hermes/.env without printing it
  python3 - "$HERMES_HOME/.env" <<'PY' 2>/dev/null
import sys,pathlib
k="TELEGRAM_BOT_"+"TOKEN"
p=pathlib.Path(sys.argv[1])
if p.exists():
  for ln in p.read_text().splitlines():
    if ln.startswith(k+"="): print(ln.split("=",1)[1].strip()); break
PY
}

echo "${B}🇮🇳📡 Telegram unblock — setup${X}"
echo "${D}Keeps your bot reachable when your ISP blocks api.telegram.org.${X}"

# ---------- 1. diagnose ----------
step "Checking what's going on"
DIRECT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 https://api.telegram.org/ 2>/dev/null)
NET=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 https://www.google.com 2>/dev/null)
if [ "$DIRECT" != "000" ]; then
  ok "api.telegram.org is reachable right now (HTTP $DIRECT)."
  echo "  ${D}The block may be intermittent. Setting this up now means you're covered when it returns.${X}"
elif [ "$NET" = "200" ]; then
  warn "api.telegram.org is BLOCKED (000) but the rest of the internet is fine — classic ISP block."
else
  err "Your whole internet looks down (google=$NET). Fix connectivity first, then re-run."; exit 1
fi

# ---------- 2. choose ----------
if [ -z "$MODE" ]; then
  step "Pick your fix"
  echo "  ${B}1) Cloudflare Worker${X}  ${G}(recommended)${X} — fast (sub-second), permanent, free. ~3 min, needs a free Cloudflare login."
  echo "  ${B}2) Auto proxy${X}         — zero setup, works now, but slower and rides free proxies."
  echo "  ${D}Tip: you can do BOTH — Worker as primary, proxy as automatic backup.${X}"
  CH=$(ask "Choose 1 or 2 [1]:" "1")
  case "$CH" in 2) MODE=proxy;; *) MODE=worker;; esac
fi

install_failover() { # install the SOCKS5 self-healer (also the Worker's backup)
  step "Installing the self-healing safety net"
  bash "$HERE/install.sh" >/dev/null 2>&1 && ok "Safety net installed (checks every 5 min, auto-heals)." \
    || warn "Safety net install hit a snag — you can run ./install.sh manually later."
}

wire_hermes() { # $1 = worker base url root (https://x.workers.dev)
  local base="$1"
  if ! command -v hermes >/dev/null 2>&1; then
    warn "Couldn't find the 'hermes' command to auto-wire. Add these yourself:"
    echo "    hermes config set gateway.platforms.telegram.extra.base_url ${base}/bot"
    echo "    hermes config set gateway.platforms.telegram.extra.base_file_url ${base}/file/bot"
    return 1
  fi
  hermes config set gateway.platforms.telegram.extra.base_url "${base}/bot" >/dev/null 2>&1
  hermes config set gateway.platforms.telegram.extra.base_file_url "${base}/file/bot" >/dev/null 2>&1
  ok "Pointed Hermes at the Worker (base_url + base_file_url)."
}

restart_and_verify() { # $1 = base url root
  local base="$1"
  step "Restarting the gateway & verifying"
  launchctl kickstart -k "gui/$(id -u)/$GATEWAY_LABEL" 2>/dev/null || true
  printf "  waiting for Telegram to connect"
  for _ in $(seq 1 12); do printf "."; sleep 3
    if grep -q "Connected to Telegram (polling mode)" "$HERMES_HOME/logs/gateway.log" 2>/dev/null \
       && [ "$(tail -200 "$HERMES_HOME/logs/gateway.log" 2>/dev/null | grep -c 'Using custom Telegram base_url')" -ge 1 ]; then
      echo; ok "Gateway connected to Telegram through the Worker."; break; fi
  done
  echo
  # live end-to-end proof via getMe through the worker
  local tok; tok="$(bot_token)"
  if [ -n "$tok" ]; then
    local got; got=$(curl -s -A "tg-setup/1.0" --max-time 12 "${base}/bot${tok}/getMe" 2>/dev/null)
    if echo "$got" | grep -q '"ok":[[:space:]]*true'; then
      local uname; uname=$(echo "$got" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')
      ok "Live check passed — Bot API answered through the Worker (@$uname)."
    else warn "Live getMe didn't confirm — check $HERMES_HOME/logs/gateway.log"; fi
  fi
}

# ============================================================ WORKER PATH ===
if [ "$MODE" = "worker" ]; then
  step "Setting up the Cloudflare Worker"

  # auth: prefer an env token, else wrangler, else guide the user
  if [ -n "${CF_API_TOKEN:-}" ] && [ -n "${CF_ACCOUNT_ID:-}" ]; then
    ok "Using CF_API_TOKEN + CF_ACCOUNT_ID from your environment."
    OUT=$(bash "$HERE/cf-worker/deploy.sh" 2>&1) || { err "Deploy failed:"; echo "$OUT"; exit 1; }
  elif command -v wrangler >/dev/null 2>&1; then
    ok "Found wrangler — deploying with it (a browser login may pop up once)."
    OUT=$(bash "$HERE/cf-worker/deploy.sh" --wrangler 2>&1) || { err "Deploy failed:"; echo "$OUT"; exit 1; }
  else
    echo "  To deploy, you need ONE of these (both free):"
    echo "    ${B}A.${X} Install wrangler:  ${C}npm i -g wrangler && wrangler login${X}   then re-run this."
    echo "    ${B}B.${X} Or paste a Cloudflare API token (no install):"
    echo "       ${D}1. Open https://dash.cloudflare.com/profile/api-tokens${X}"
    echo "       ${D}2. Create Token → use the \"Edit Cloudflare Workers\" template → Create${X}"
    echo "       ${D}3. Also copy your Account ID (in the dashboard URL: dash.cloudflare.com/<ID>/...)${X}"
    TKN=$(ask "Paste API token (or leave blank to switch to the proxy option):" "")
    if [ -z "$TKN" ]; then warn "No token — switching to the zero-setup proxy option."; MODE=proxy
    else
      ACC=$(ask "Paste Account ID:" "")
      [ -z "$ACC" ] && { err "Account ID required."; exit 1; }
      OUT=$(CF_API_TOKEN="$TKN" CF_ACCOUNT_ID="$ACC" bash "$HERE/cf-worker/deploy.sh" 2>&1) \
        || { err "Deploy failed:"; echo "$OUT"; exit 1; }
    fi
  fi

  if [ "$MODE" = "worker" ]; then
    echo "$OUT" | sed 's/^/    /'
    BASE=$(echo "$OUT" | sed -n 's#.*\(https://[a-z0-9.-]*\.workers\.dev\)/bot.*#\1#p' | head -1)
    [ -z "$BASE" ] && { err "Couldn't parse the Worker URL from the deploy output above."; exit 1; }
    ok "Worker is live at ${B}$BASE${X}"
    wire_hermes "$BASE"
    install_failover            # keep the SOCKS5 layer as automatic backup
    restart_and_verify "$BASE"
    step "All set ${G}✓${X}"
    echo "  Primary path : ${B}Cloudflare Worker${X} (fast, permanent)"
    echo "  Backup path  : SOCKS5 self-healer (auto, only if the Worker is ever down)"
    echo "  Status any time: ${C}python3 $HERMES_HOME/telegram-india-unblock/telegram_proxy_failover.py --status${X}"
    exit 0
  fi
fi

# ============================================================= PROXY PATH ===
if [ "$MODE" = "proxy" ]; then
  step "Setting up the zero-config self-healer"
  install_failover
  step "All set ${G}✓${X}"
  echo "  Your bot now routes through a live SOCKS5 exit when the block is on,"
  echo "  rotates automatically as free proxies die, and goes back to a direct"
  echo "  connection the moment the block lifts. Checks every 5 minutes."
  echo
  echo "  Status any time: ${C}python3 $HERMES_HOME/telegram-india-unblock/telegram_proxy_failover.py --status${X}"
  echo "  ${D}Want it faster + permanent later? Re-run and pick option 1 (Cloudflare Worker).${X}"
  exit 0
fi
