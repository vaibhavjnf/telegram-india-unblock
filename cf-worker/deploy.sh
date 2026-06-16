#!/usr/bin/env bash
# deploy.sh — deploy the Telegram Bot API reverse-proxy Worker to Cloudflare,
# enable its workers.dev URL, and print the base_url to point your bot at.
#
# Two ways to authenticate (pick one):
#   1. wrangler (recommended): `npm i -g wrangler && wrangler login`, then:
#        bash deploy.sh --wrangler
#   2. API token (no install): create a token with the "Edit Cloudflare Workers"
#      template at https://dash.cloudflare.com/profile/api-tokens, then:
#        CF_API_TOKEN=xxxx CF_ACCOUNT_ID=xxxx bash deploy.sh
#
# Result: https://<NAME>.<your-subdomain>.workers.dev  — set your bot's API
# base_url to  <that-url>/bot  (and base_file_url to <that-url>/file/bot).
set -euo pipefail

NAME="${WORKER_NAME:-tg-proxy}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- wrangler path ----------
if [ "${1:-}" = "--wrangler" ]; then
  command -v wrangler >/dev/null || { echo "wrangler not found: npm i -g wrangler && wrangler login"; exit 1; }
  cat > "$HERE/wrangler.toml" <<EOF
name = "$NAME"
main = "worker.js"
compatibility_date = "2025-01-01"
workers_dev = true
EOF
  ( cd "$HERE" && wrangler deploy )
  echo "Deployed. Your URL is printed above (…workers.dev). Use <url>/bot as the bot base_url."
  exit 0
fi

# ---------- API-token path (pure curl, no install) ----------
: "${CF_API_TOKEN:?set CF_API_TOKEN (token with 'Edit Cloudflare Workers' permission)}"
: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID (Cloudflare account id)}"
API="https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID"

# curl header config so the token is never echoed on the command line
HDR="$(mktemp)"; trap 'rm -f "$HDR"' EXIT
{ printf 'header = "Authorization: Bearer '; printf '%s' "$CF_API_TOKEN"; printf '"\n'; } > "$HDR"
chmod 600 "$HDR"

echo "[1/3] Uploading Worker '$NAME'..."
printf '{"main_module":"worker.js","compatibility_date":"2025-01-01"}' > /tmp/_meta.json
curl -fsS -K "$HDR" -X PUT "$API/workers/scripts/$NAME" \
  -F "metadata=@/tmp/_meta.json;type=application/json" \
  -F "worker.js=@$HERE/worker.js;type=application/javascript+module" \
  | grep -q '"success":[[:space:]]*true' && echo "    ok" || { echo "    upload failed"; exit 1; }

echo "[2/3] Enabling workers.dev route..."
curl -fsS -K "$HDR" -X POST "$API/workers/scripts/$NAME/subdomain" \
  -H "Content-Type: application/json" --data '{"enabled":true}' >/dev/null
SUB=$(curl -fsS -K "$HDR" "$API/workers/subdomain" | sed -n 's/.*"subdomain":[[:space:]]*"\([^"]*\)".*/\1/p')

echo "[3/3] Done."
echo
echo "  Worker URL : https://$NAME.$SUB.workers.dev"
echo "  base_url   : https://$NAME.$SUB.workers.dev/bot"
echo "  base_file  : https://$NAME.$SUB.workers.dev/file/bot"
echo
echo "Hermes: hermes config set gateway.platforms.telegram.extra.base_url https://$NAME.$SUB.workers.dev/bot"
echo "        hermes config set gateway.platforms.telegram.extra.base_file_url https://$NAME.$SUB.workers.dev/file/bot"
echo "        then restart the gateway."
