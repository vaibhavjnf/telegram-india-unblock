# telegram-india-unblock 🇮🇳🚫📡

**Your bot went silent. It's not your code. India blocked Telegram again.**

A self-healing hotfix that keeps a [Hermes](https://github.com/NousResearch/hermes-agent) (Claude Code) gateway's **Telegram** channel alive when your ISP blocks `api.telegram.org`.

It ships **two solutions** — use the first if you can spend 3 minutes once, the second if you want zero setup:

| | **① Cloudflare Worker** (recommended) | **② SOCKS5 self-healer** (fallback) |
|---|---|---|
| What | A reverse-proxy on Cloudflare's edge; point your bot's `base_url` at it | A timer that routes the Bot API through a live free SOCKS5 exit |
| Speed | **sub-second**, served from the nearest Cloudflare PoP | 2–25s (free-proxy cold handshake) |
| Reliability | **permanent** — doesn't rot, can't be ISP-blocked | self-heals as free exits die/rotate |
| Setup | one 3-minute deploy ([`cf-worker/`](cf-worker/)) | one command, no accounts |
| Maintenance | none | none (automated) |

**They compose.** Deploy the Worker as your primary path and keep the self-healer installed: it detects the Worker, **stands down** while the Worker is healthy, and only falls back to SOCKS5 if the Worker ever becomes unreachable. Defense in depth, fully automatic.

→ **Recommended:** start with [`cf-worker/README.md`](cf-worker/README.md). The SOCKS5 layer below is the zero-setup option / backup.

Pure stdlib Python + `curl`. No new dependencies. Goes back to a direct connection on its own the moment the block lifts.

---

## ⚡ Quick start — one command

```bash
git clone https://github.com/vaibhavjnf/telegram-india-unblock.git
cd telegram-india-unblock
bash setup.sh
```

That's it. `setup.sh` walks you through it in plain language:

1. **Checks** whether your ISP is actually blocking Telegram (vs. a real bug).
2. **Asks** which fix you want — Cloudflare Worker (fast & permanent) or the zero-setup auto-proxy. Press Enter for the recommended one.
3. **Deploys + wires it up** for you (sets Hermes' `base_url`, installs the safety net).
4. **Restarts and verifies** end-to-end — it won't say "done" until a real Bot API call answers through the new path.

```
✓ api.telegram.org is BLOCKED but the rest of the internet is fine — classic ISP block.
✓ Worker is live at https://tg-proxy.yourname.workers.dev
✓ Pointed Hermes at the Worker
✓ Gateway connected to Telegram through the Worker.
✓ Live check passed — Bot API answered through the Worker (@yourbot).
▸ All set ✓
```

Non-interactive (CI / scripted):
```bash
CF_API_TOKEN=*** CF_ACCOUNT_ID=xxx bash setup.sh --worker --yes   # Cloudflare Worker
bash setup.sh --proxy --yes                                       # auto-proxy only
```

Prefer to understand the two options first? Read on.

---

## The story: India has gone mad

On a normal Tuesday in June 2026, half of India woke up to dead Telegram bots, dead Telegram apps, dead `web.telegram.org`. Not a Telegram outage — Telegram was up everywhere else on Earth. **Indian ISPs (Jio and friends) were null-routing Telegram's IP ranges again.**

This is not new. India has a long, exhausting habit of blocking Telegram in waves — sometimes for "exam leaks," sometimes for "national security," sometimes for nothing anyone will put in writing. The block is crude: drop TCP to the IP blocks that serve `api.telegram.org`. Your DNS still resolves. Every other site loads. Only Telegram dies.

If you run an always-on agent (a Hermes/Claude Code gateway, a notification bot, anything that *lives* on the Telegram Bot API), the symptom looks like a bug in **your** stack:

```
[Telegram] Primary api.telegram.org connection failed (); trying fallback IPs ...
[Telegram] Fallback IP 149.154.167.220 failed:
[Telegram] Primary api.telegram.org connection failed (); trying fallback IPs ...
```

…on an infinite loop. You'll burn an hour checking your token, your polling, your config. It's none of those. **It's the country.**

```
$ curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org/
000                      # every Bot API IP, IPv4 + IPv6
$ curl -s -o /dev/null -w "%{http_code}\n" https://www.google.com
200                      # the rest of the internet is fine
```

So this repo does the only sane thing: **tunnel just the Telegram Bot API through a working exit, and automate the whole mess** so you never think about it again.

---

## What it does

A single script, `telegram_proxy_failover.py`, run on a 5-minute timer. Every tick, idempotently:

| Situation | Action |
|---|---|
| `api.telegram.org` reachable **directly** | Remove the proxy, go direct, restart gateway once. (Block lifted → back to normal.) |
| Blocked, current proxy **still works** + poll flowing | **Do nothing.** No restart, no churn. |
| Blocked, proxy **reachable but the poll has wedged** (gateway went silent ≥ 8 min) | Force **one** restart to re-establish a clean long-poll. Cooldown prevents a restart loop. |
| Blocked, current proxy **dead / unset** | Pick the **fastest live** SOCKS5 exit from the pool, write `TELEGRAM_PROXY`, restart gateway once. |
| Blocked, whole pool **dead** | Fetch a fresh free SOCKS5 list (per country), find a live one, use it. |

It only restarts your gateway when the working proxy **actually changes** (or to clear a wedged poll, at most once per cooldown window), so running it every 5 minutes is cheap and quiet.

### The "silent wedge" problem (why v2 exists)

Free SOCKS5 exits are flaky. A marginal one (~1-in-3 failure) can **stall the Telegram long-poll with no error at all** — the proxy still tests healthy, but the gateway goes quiet and stops replying. v1 only rotated on a *fully dead* proxy, so it missed this. v2 watches the gateway log: if Telegram has been silent past a threshold (`TGFAILOVER_MAX_SILENT_MIN`, default 8) while the gateway is up and a proxy is reachable, it forces a single clean restart. It also now **ranks exits by latency** and picks the fastest, not just the first that answers.

Hermes already knows how to use `TELEGRAM_PROXY` (both the in-gateway polling adapter and the standalone `send_message` path read it and pass it to `httpx` / `python-telegram-bot`). This hotfix just keeps that variable pointed at something that *works*, forever, without you.

### Proxy-friendly timeouts (important)

Free SOCKS5 exits pass a quick `curl` but their **cold connect handshake is slow** — often 10-25s. Hermes' default Telegram connect timeout is only 10s, so the gateway can keep *timing out the connect* even though the proxy is fine for short requests (you'll see `Connect attempt N/8 failed: Timed out` looping). The installer therefore also sets, in `~/.hermes/.env`:

```
HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT=30   # default 10 — give the proxy time to handshake
HERMES_TELEGRAM_HTTP_POOL_TIMEOUT=20      # default 8
HERMES_TELEGRAM_HTTP_READ_TIMEOUT=40      # default 20
```

These are stock Hermes knobs; they just need loosening when a proxy sits in the path. Without them, a perfectly reachable proxy can look "dead" to the gateway.

---

## Install

```bash
git clone https://github.com/vaibhavjnf/telegram-india-unblock.git
cd telegram-india-unblock
bash install.sh
```

or one-shot:

```bash
curl -fsSL https://raw.githubusercontent.com/vaibhavjnf/telegram-india-unblock/main/install.sh | bash
```

That:
1. Copies the script + proxy pool into `~/.hermes/telegram-india-unblock/`
2. Runs an immediate heal (your bot comes back **now**)
3. Installs a **launchd** timer (macOS) or **cron** entry (Linux), every 5 min, also at login/reboot

### Status / logs / uninstall

```bash
python3 ~/.hermes/telegram-india-unblock/telegram_proxy_failover.py --status
tail -f ~/.hermes/logs/telegram-proxy-failover.log
bash install.sh --uninstall      # removes timer + proxy line, goes direct
```

`--status` example:

```
direct api.telegram.org reachable : no (ISP block)
TELEGRAM_PROXY in ~/.hermes/.env  : socks5h://51.79.177.162:1010
current proxy reaches Bot API     : yes (exit SG)
overall                           : HEALTHY
```

---

## How it works (the short version)

- **Detection:** `curl` the Bot API directly. `000` = blocked. Any of `200/301/302/404` back = the wire reached Telegram.
- **Proxy:** `TELEGRAM_PROXY=socks5h://host:port` in `~/.hermes/.env`. `socks5h` = remote DNS through the proxy (so the *exit* resolves Telegram, not your polluted local resolver).
- **Pool:** `proxy_pool.txt`, lines of `CC host:port`. Country-priority order — **SG first** (closest to India ≈ lowest latency), then US. Free exits rot; the script auto-fetches fresh ones ([proxifly](https://github.com/proxifly/free-proxy-list)) and appends the live finds.
- **Restart:** only when the `.env` value changes, via `launchctl kickstart` (macOS) — your gateway re-reads the proxy on boot.

No bridge process needed — `httpx` (Hermes' HTTP client) speaks `socks5h://` natively via `socksio`, which Hermes already ships.

---

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes home (where `.env` and `logs/` live) |
| `TGFAILOVER_GATEWAY_LABEL` | `ai.hermes.gateway` | launchd label to kickstart on change |
| `TGFAILOVER_COUNTRIES` | `SG US` | exit-country priority |
| `TGFAILOVER_POOL_FILE` | `./proxy_pool.txt` | the SOCKS5 pool |
| `TGFAILOVER_ENV_FILE` | `$HERMES_HOME/.env` | where `TELEGRAM_PROXY` is written |
| `TGFAILOVER_GATEWAY_LOG` | `$HERMES_HOME/logs/gateway.log` | log watched for a wedged (silent) poll |
| `TGFAILOVER_MAX_SILENT_MIN` | `8` | force a restart if Telegram is silent this long while a proxy is reachable |
| `TGFAILOVER_RESTART_COOLDOWN_MIN` | `12` | min minutes between forced restarts (anti-loop) |

Multiple Hermes profiles? Point `TGFAILOVER_ENV_FILE` / `HERMES_HOME` at each profile's `.env` and install one timer per profile (or just heal them all from one script — PRs welcome).

---

## A note on free proxies

Free SOCKS5 exits are **unreliable by nature** — slow, rate-limited, here-today-gone-tomorrow. That's fine for keeping a long-lived **polling** connection alive (it tolerates a slow exit), and acceptable for bot replies. If you want rock-solid latency, drop your own paid exit / VPS / Tailscale exit-node into `proxy_pool.txt` as `SG your.host:port` and it'll be preferred. The automation is identical; only the quality of the pool changes.

---

## Why this exists / who it's for

Anyone in a region that intermittently blocks Telegram (India especially, but the pattern is general) running an always-on bot or agent. Built against [Hermes / Claude Code](https://github.com/NousResearch/hermes-agent), but the core idea — *detect block → tunnel only the Bot API → self-heal → go direct when it lifts* — applies to any `python-telegram-bot` / `httpx`-based service.

---

## License

MIT. Use it, fork it, ship it.

Built by **Vaibhav Sharma** · X [@vabbyshabby](https://x.com/vabbyshabby) · while India was, once again, having a moment.
