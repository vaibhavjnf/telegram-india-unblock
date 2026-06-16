#!/usr/bin/env python3
"""
telegram_proxy_failover.py — keep a Hermes gateway's Telegram channel alive
when an ISP blocks api.telegram.org (hello, India 🇮🇳).

WHAT IT DOES (idempotent, safe to run every few minutes):
  1. If api.telegram.org is reachable DIRECTLY, it removes any TELEGRAM_PROXY
     line from ~/.hermes/.env and restarts the gateway (block lifted -> go direct).
  2. Otherwise it tests the currently-configured TELEGRAM_PROXY. If it still
     reaches the Bot API, it does NOTHING (no restart, no churn).
  3. If the current proxy is dead (or unset), it picks the first LIVE SOCKS5
     exit from the pool, writes TELEGRAM_PROXY into ~/.hermes/.env, and restarts
     the gateway exactly once.
  4. If the whole seed pool is dead, it fetches a fresh free SOCKS5 list
     (proxifly) for the preferred countries and retries.

It only restarts the gateway when the .env value actually CHANGES, so running
it on a 5-minute timer is cheap and quiet.

Pure stdlib. Uses the system `curl` for proxy tests (curl speaks socks5h://
everywhere on macOS/Linux, no python socks dependency needed).

Usage:
  telegram_proxy_failover.py            # heal: ensure a working Telegram path
  telegram_proxy_failover.py --check    # report only, exit 0=healthy 1=needs-heal, no changes
  telegram_proxy_failover.py --status   # human-readable status, no changes
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---- config (env-overridable; sane defaults for a standard Hermes install) ----
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
ENV_FILE = Path(os.environ.get("TGFAILOVER_ENV_FILE", str(HERMES_HOME / ".env")))
POOL_FILE = Path(
    os.environ.get(
        "TGFAILOVER_POOL_FILE", str(Path(__file__).resolve().parent / "proxy_pool.txt")
    )
)
GATEWAY_LABEL = os.environ.get("TGFAILOVER_GATEWAY_LABEL", "ai.hermes.gateway")
# Country priority for picking an exit. SG first (closest to India, low latency).
COUNTRIES = os.environ.get("TGFAILOVER_COUNTRIES", "SG US").split()
# How we know a SOCKS5 exit can carry the Bot API: any of these HTTP codes back
# from https://api.telegram.org/ means the wire reached Telegram (302/404/200).
_OK_CODES = {"200", "301", "302", "404"}
TG_PROBE_URL = "https://api.telegram.org/"
LOG = HERMES_HOME / "logs" / "telegram_proxy_failover.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- probes ----
def _curl_code(url: str, proxy: str | None, timeout: int = 10) -> str:
    """Return the HTTP code curl gets for url (through proxy if given), or '000'."""
    cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "--max-time", str(timeout)]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 4)
        return (out.stdout or "000").strip() or "000"
    except (subprocess.TimeoutExpired, OSError):
        return "000"


def direct_telegram_ok() -> bool:
    return _curl_code(TG_PROBE_URL, None, timeout=8) in _OK_CODES


def proxy_reaches_telegram(proxy: str) -> bool:
    if not proxy:
        return False
    # try twice; free exits are flaky and a single timeout shouldn't condemn one
    for _ in range(2):
        if _curl_code(TG_PROBE_URL, proxy, timeout=12) in _OK_CODES:
            return True
    return False


def _exit_country(proxy: str) -> str | None:
    out = _curl_code  # noqa  (keep linter calm)
    try:
        cmd = ["curl", "-s", "--max-time", "8", "-x", proxy, "https://www.cloudflare.com/cdn-cgi/trace"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        m = re.search(r"^loc=([A-Z]{2})", res.stdout or "", re.M)
        return m.group(1) if m else None
    except (subprocess.TimeoutExpired, OSError):
        return None


# ------------------------------------------------------------- pool I/O ----
def read_pool() -> list[tuple[str, str]]:
    """Return [(CC, host:port), ...] from the pool file."""
    out: list[tuple[str, str]] = []
    if not POOL_FILE.exists():
        return out
    for raw in POOL_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            out.append((parts[0].upper(), parts[1]))
        elif len(parts) == 1 and ":" in parts[0]:
            out.append(("??", parts[0]))
    return out


def append_pool(cc: str, hostport: str) -> None:
    existing = {hp for _, hp in read_pool()}
    if hostport in existing:
        return
    try:
        with POOL_FILE.open("a") as f:
            f.write(f"{cc} {hostport}\n")
    except OSError:
        pass


def fetch_fresh(country: str) -> list[str]:
    """Fetch a fresh free SOCKS5 list for `country` (proxifly mirror)."""
    url = (
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/"
        f"proxies/countries/{country}/data.json"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            import json

            data = json.load(r)
        hps = []
        for x in data:
            if x.get("protocol") == "socks5":
                p = x.get("proxy", "")
                if "://" in p:
                    hps.append(p.split("://", 1)[1])
        return hps
    except Exception:
        return []


def pick_live_exit() -> str | None:
    """Find a SOCKS5 exit (socks5h://host:port) that actually reaches the Bot API."""
    # 1) seed pool, in country priority order
    pool = read_pool()
    for want in COUNTRIES:
        for cc, hp in pool:
            if cc != want:
                continue
            proxy = f"socks5h://{hp}"
            if proxy_reaches_telegram(proxy):
                log(f"[pick] seed {hp} ({cc}) reaches Telegram")
                return proxy
    # 2) seed dry -> fetch fresh per country
    for want in COUNTRIES:
        log(f"[pick] seed dry for {want}; fetching fresh list...")
        for hp in fetch_fresh(want)[:40]:
            proxy = f"socks5h://{hp}"
            cc = _exit_country(proxy)
            if cc == want and proxy_reaches_telegram(proxy):
                log(f"[pick] fresh {hp} ({cc}) reaches Telegram")
                append_pool(want, hp)
                return proxy
    return None


# -------------------------------------------------------------- env I/O ----
_ENV_RE = re.compile(r"^TELEGRAM_PROXY=(.*)$", re.M)


def current_proxy() -> str:
    if not ENV_FILE.exists():
        return ""
    m = _ENV_RE.search(ENV_FILE.read_text())
    return (m.group(1).strip() if m else "").strip()


def set_proxy(value: str | None) -> bool:
    """Write TELEGRAM_PROXY=value (or remove the line if value is None).
    Returns True if the file actually changed."""
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    had = current_proxy()
    # strip any existing line(s) + our marker comment
    text = "\n".join(
        ln for ln in text.splitlines()
        if not ln.startswith("TELEGRAM_PROXY=")
        and "# telegram-india-unblock" not in ln
    )
    if value:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "# telegram-india-unblock: ISP blocks api.telegram.org, route via SOCKS5 exit\n"
        text += f"TELEGRAM_PROXY={value}\n"
    changed = (value or "") != had
    if changed:
        backup = ENV_FILE.with_suffix(ENV_FILE.suffix + f".bak-tgproxy-{int(time.time())}")
        try:
            if ENV_FILE.exists():
                backup.write_text(ENV_FILE.read_text())
        except OSError:
            pass
        ENV_FILE.write_text(text if text.endswith("\n") else text + "\n")
    return changed


def restart_gateway() -> None:
    uid = os.getuid()
    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{GATEWAY_LABEL}"]
    log(f"[restart] {' '.join(cmd)}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"[restart] failed: {e}")


# ----------------------------------------------------------------- main ----
def heal() -> int:
    cur = current_proxy()
    # 1) direct path healthy -> drop the proxy, go direct
    if direct_telegram_ok():
        if cur:
            log("[heal] direct api.telegram.org reachable -> removing proxy, going direct")
            if set_proxy(None):
                restart_gateway()
        else:
            log("[heal] direct reachable, no proxy set -> healthy, nothing to do")
        return 0
    # 2) blocked. current proxy still works?
    if cur and proxy_reaches_telegram(cur):
        log(f"[heal] direct blocked; current proxy {cur} healthy -> nothing to do")
        return 0
    # 3) need a (new) live exit
    log(f"[heal] direct blocked; current proxy {'DEAD: ' + cur if cur else 'unset'} -> rotating")
    new = pick_live_exit()
    if not new:
        log("[heal] NO live SG/US SOCKS5 exit found. Telegram stays down until a proxy is reachable.")
        return 2
    if set_proxy(new):
        log(f"[heal] TELEGRAM_PROXY -> {new}; restarting gateway")
        restart_gateway()
    else:
        log(f"[heal] proxy unchanged ({new}); no restart")
    return 0


def check() -> int:
    cur = current_proxy()
    if direct_telegram_ok():
        return 0 if not cur else 1  # if direct works but a (maybe stale) proxy is set, suggest heal
    if cur and proxy_reaches_telegram(cur):
        return 0
    return 1


def status() -> int:
    cur = current_proxy()
    direct = direct_telegram_ok()
    print(f"direct api.telegram.org reachable : {'yes' if direct else 'no (ISP block)'}")
    print(f"TELEGRAM_PROXY in {ENV_FILE}     : {cur or '(unset)'}")
    if cur:
        ok = proxy_reaches_telegram(cur)
        cc = _exit_country(cur) if ok else None
        print(f"current proxy reaches Bot API     : {'yes' if ok else 'NO'}{f' (exit {cc})' if cc else ''}")
    healthy = (direct and not cur) or (cur and proxy_reaches_telegram(cur))
    print(f"overall                           : {'HEALTHY' if healthy else 'NEEDS HEAL'}")
    return 0 if healthy else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only (exit 1 if needs heal), no changes")
    ap.add_argument("--status", action="store_true", help="human-readable status, no changes")
    args = ap.parse_args()
    if args.status:
        return status()
    if args.check:
        return check()
    return heal()


if __name__ == "__main__":
    sys.exit(main())
