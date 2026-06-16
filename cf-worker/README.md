# cf-worker — the fast, reliable path (recommended)

A **Cloudflare Worker reverse-proxy** for the Telegram Bot API. This is the
primary, professional fix for an ISP block of `api.telegram.org`; the SOCKS5
self-healer in the parent directory is the zero-setup fallback.

## Why this beats a proxy

| | Cloudflare Worker | Free SOCKS5 proxy |
|---|---|---|
| Latency | **sub-second** (served from the nearest Cloudflare PoP) | 2–25s cold handshake |
| Reliability | **does not rot** — no pool, no rotation | free exits die hourly |
| Blockable by ISP | no (ISPs can't wholesale-block Cloudflare) | individual exits get blocked |
| Maintenance | **none** after deploy | constant (rotate, re-probe, self-heal) |
| Cost | free (100k req/day) | free, but flaky |

It's the same approach **500+ bots in Iran** use, with sub-100ms average. A
Worker runs *on* Cloudflare's edge (which reaches Telegram fine) and is served
from Cloudflare's global anycast (which your ISP can't block without breaking a
large fraction of the web).

## How it works

```
your bot  ──HTTPS──▶  https://<name>.<sub>.workers.dev/bot<token>/getUpdates
                          │  (Cloudflare edge — reachable from a blocked ISP)
                          ▼
                      api.telegram.org   (reached from Cloudflare, not from you)
```

`worker.js` just rewrites the host to `api.telegram.org`, strips edge-identity
headers, and streams the response straight back. Telegram never sees your IP;
your ISP never sees a Telegram connection.

## Deploy (under 3 minutes)

### Option A — wrangler (recommended)
```bash
npm i -g wrangler && wrangler login      # one browser OAuth
cd cf-worker && bash deploy.sh --wrangler
```

### Option B — API token (no install, pure curl)
1. Create a token with the **“Edit Cloudflare Workers”** template at
   <https://dash.cloudflare.com/profile/api-tokens> (Account → Workers Scripts → Edit).
2. Grab your account id from the dashboard URL (`dash.cloudflare.com/<ACCOUNT_ID>/…`).
3. ```bash
   CF_API_TOKEN=*** CF_ACCOUNT_ID=your_account_id bash deploy.sh
   ```

Either way you get a URL like `https://tg-proxy.yourname.workers.dev`.

## Point your bot at it

**Hermes:**
```bash
hermes config set gateway.platforms.telegram.extra.base_url      https://<name>.<sub>.workers.dev/bot
hermes config set gateway.platforms.telegram.extra.base_file_url https://<name>.<sub>.workers.dev/file/bot
# restart the gateway
```
You'll see in the log: `[Telegram] Using custom Telegram base_url: https://…workers.dev/bot` → `Connected to Telegram (polling mode)`.

**python-telegram-bot (generic):**
```python
from telegram.ext import ApplicationBuilder
app = (ApplicationBuilder()
       .token(TOKEN)
       .base_url("https://<name>.<sub>.workers.dev/bot")
       .base_file_url("https://<name>.<sub>.workers.dev/file/bot")
       .build())
```

**raw API / curl:** replace `https://api.telegram.org` with your Worker URL.

## Verify

```bash
curl -A test "https://<name>.<sub>.workers.dev/bot<token>/getMe"
# → {"ok":true,"result":{...}}
```

> Note: send a `User-Agent` header. Cloudflare's default bot-fight can 403 a
> request with an empty UA. Every real Telegram client (httpx, PTB, curl -A)
> sends one, so the gateway is unaffected; only a bare `urllib` call needs it.

## Security model

The Worker forwards any `/bot<token>/…` request, so the URL is exactly as secret
as the token in the path — **the token IS the credential**, identical to
`api.telegram.org`. Don't publish URL+token. For defense in depth, uncomment the
`SECRET_PREFIX` gate in `worker.js` and put that prefix in your base_url.

## Files
- `worker.js` — the Worker (module syntax, zero dependencies)
- `deploy.sh` — one-shot deploy (wrangler or API-token path)
- `wrangler.toml` — wrangler config

Built by **Vaibhav Sharma** (X: [@vabbyshabby](https://x.com/vabbyshabby)).
