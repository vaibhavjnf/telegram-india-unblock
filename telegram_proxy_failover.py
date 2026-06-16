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
# Gateway log + "silent wedge" detection. A flaky-but-alive proxy can stall the
# long-poll with NO error, so the proxy still tests healthy while the gateway
# has gone quiet. If the gateway is up, a proxy reaches Telegram, yet there has
# been no Telegram log activity for this many minutes, we force one restart.
GATEWAY_LOG = Path(os.environ.get("TGFAILOVER_GATEWAY_LOG", str(HERMES_HOME / "logs" / "gateway.log")))
MAX_SILENT_MIN = float(os.environ.get("TGFAILOVER_MAX_SILENT_MIN", "8"))


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


def proxy_latency(proxy: str, timeout: int = 12) -> float | None:
    """Best round-trip time (s) to the Bot API over `proxy` across a few tries,
    or None if it never reached Telegram. Used to RANK exits, fastest-first —
    a marginal proxy that only sometimes answers stalls the long-poll."""
    best: float | None = None
    for _ in range(3):
        cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code} %{time_total}",
               "--max-time", str(timeout), "-x", proxy, TG_PROBE_URL]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 4)
            parts = (res.stdout or "000 0").split()
            if len(parts) == 2 and parts[0] in _OK_CODES:
                t = float(parts[1])
                if best is None or t < best:
                    best = t
        except (subprocess.TimeoutExpired, OSError, ValueError):
            continue
    return best


def gateway_running() -> bool:
    try:
        res = subprocess.run(["launchctl", "list", GATEWAY_LABEL],
                             capture_output=True, text=True, timeout=8)
        # exit 0 + a numeric PID line => loaded and running
        return res.returncode == 0 and bool(re.search(r'"PID"\s*=\s*\d+', res.stdout or ""))
    except (subprocess.TimeoutExpired, OSError):
        return False


_TG_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def telegram_silent_minutes() -> float | None:
    """Minutes since the last Telegram log line in the gateway log, or None if
    we can't tell (no log / no telegram lines). A wedged long-poll shows up as
    a long gap here even though the proxy still tests healthy."""
    if not GATEWAY_LOG.exists():
        return None
    last_ts = None
    try:
        # tail the file cheaply (read last ~256 KB)
        size = GATEWAY_LOG.stat().st_size
        with GATEWAY_LOG.open("rb") as f:
            if size > 262144:
                f.seek(-262144, os.SEEK_END)
            chunk = f.read().decode("utf-8", "replace")
        for ln in chunk.splitlines():
            if "telegram" in ln.lower():
                m = _TG_LOG_TS.match(ln)
                if m:
                    last_ts = m.group(1)
    except OSError:
        return None
    if not last_ts:
        return None
    try:
        t = time.mktime(time.strptime(last_ts, "%Y-%m-%d %H:%M:%S"))
        return max(0.0, (time.time() - t) / 60.0)
    except (ValueError, OverflowError):
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


def pick_live_exit(min_latency_gain: float = 0.0) -> str | None:
    """Find the FASTEST SOCKS5 exit (socks5h://host:port) that reaches the Bot
    API. Probes candidates CONCURRENTLY and ranks by round-trip latency,
    fastest-first, so we don't settle on a marginal exit that stalls the
    long-poll. Country-priority is a tiebreak."""
    import concurrent.futures as _cf

    def probe(item: tuple[str, str]) -> tuple[float, str, str] | None:
        cc, hp = item
        proxy = f"socks5h://{hp}"
        lat = proxy_latency(proxy, timeout=9)
        if lat is not None:
            return (lat, proxy, cc)
        return None

    def run(items: list[tuple[str, str]], fresh: bool) -> list[tuple[float, str, str]]:
        if not items:
            return []
        out: list[tuple[float, str, str]] = []
        with _cf.ThreadPoolExecutor(max_workers=20) as ex:
            for res in ex.map(probe, items):
                if res:
                    out.append(res)
                    lat, proxy, cc = res
                    log(f"[pick] {'fresh ' if fresh else ''}{proxy} ({cc}) reaches Telegram @ {lat:.2f}s")
                    if fresh:
                        append_pool(cc, proxy.split("://", 1)[1])
        return out

    # 1) probe the whole seed pool in parallel (preferred countries only)
    pool = [(cc, hp) for cc, hp in read_pool() if cc in COUNTRIES]
    candidates = run(pool, fresh=False)

    # 2) seed dry -> fetch fresh per country, probe those in parallel too
    if not candidates:
        fresh_items: list[tuple[str, str]] = []
        for want in COUNTRIES:
            log(f"[pick] seed dry for {want}; fetching fresh list...")
            fresh_items += [(want, hp) for hp in fetch_fresh(want)[:30]]
        # dedup hosts
        seen: set[str] = set()
        fresh_items = [(cc, hp) for cc, hp in fresh_items if not (hp in seen or seen.add(hp))]
        candidates = run(fresh_items, fresh=True)

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])  # fastest first
    best_lat, best_proxy, best_cc = candidates[0]
    log(f"[pick] chose {best_proxy} ({best_cc}) @ {best_lat:.2f}s (of {len(candidates)} live)")
    return best_proxy


# -------------------------------------------------------------- env I/O ----
_ENV_RE = re.compile(r"^TELEGRAM_PROXY=(.*)$", re.M)


def current_proxy() -> str:
    if not ENV_FILE.exists():
        return ""
    m = _ENV_RE.search(ENV_FILE.read_text())
    return (m.group(1).strip() if m else "").strip()


# Proxy-friendly Telegram timeouts. Free SOCKS5 exits have a slow cold connect
# handshake; Hermes' default connect timeout (10s) is too tight and the gateway
# times out the connect even though the proxy works. We ensure these are set
# (only when a proxy is active) so a reachable-but-slow proxy isn't seen as dead.
_PROXY_TIMEOUTS = {
    "HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT": "30",
    "HERMES_TELEGRAM_HTTP_POOL_TIMEOUT": "20",
    "HERMES_TELEGRAM_HTTP_READ_TIMEOUT": "40",
}


def _ensure_proxy_timeouts(text: str) -> str:
    """Ensure the proxy-friendly Telegram timeout lines exist in env text."""
    lines = text.splitlines()
    have = {ln.split("=", 1)[0] for ln in lines if "=" in ln}
    add = [f"{k}={v}" for k, v in _PROXY_TIMEOUTS.items() if k not in have]
    if not add:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    text += "# telegram-india-unblock: loosened timeouts so a slow proxy isn't seen as dead\n"
    text += "\n".join(add) + "\n"
    return text


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
        text = _ensure_proxy_timeouts(text)
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
# Restart cooldown: a wedge restart must not loop. We stamp a tiny state file
# and refuse another forced restart inside this window.
_RESTART_STAMP = HERMES_HOME / "run" / "tg_failover_last_restart"
RESTART_COOLDOWN_MIN = float(os.environ.get("TGFAILOVER_RESTART_COOLDOWN_MIN", "12"))


def _mark_restart() -> None:
    try:
        _RESTART_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_STAMP.write_text(str(int(time.time())))
    except OSError:
        pass


def _restarted_recently() -> bool:
    try:
        ts = int(_RESTART_STAMP.read_text().strip())
    except (OSError, ValueError):
        return False
    return (time.time() - ts) < RESTART_COOLDOWN_MIN * 60


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
        # 2a) proxy is reachable but is the long-poll actually flowing? A flaky
        # exit can stall polling with no error (gateway goes silent). If the
        # gateway is up and Telegram has been quiet too long, force ONE restart
        # — but only if we haven't restarted very recently (avoid a loop).
        silent = telegram_silent_minutes()
        if (
            silent is not None
            and silent >= MAX_SILENT_MIN
            and gateway_running()
            and not _restarted_recently()
        ):
            log(f"[heal] proxy {cur} reachable but Telegram silent {silent:.1f}m "
                f"(>= {MAX_SILENT_MIN}m) -> wedged long-poll, restarting once")
            # A restart re-establishes a clean poll. Don't churn the .env here —
            # if the exit is genuinely too slow it'll wedge again next window and
            # branch 3 (or the next wedge) will rotate. Keep this path cheap.
            _mark_restart()
            restart_gateway()
            return 0
        log(f"[heal] direct blocked; current proxy {cur} healthy"
            f"{f', telegram quiet {silent:.1f}m' if silent is not None else ''} -> nothing to do")
        return 0
    # 3) need a (new) live exit
    log(f"[heal] direct blocked; current proxy {'DEAD: ' + cur if cur else 'unset'} -> rotating")
    new = pick_live_exit()
    if not new:
        log("[heal] NO live SG/US SOCKS5 exit found. Telegram stays down until a proxy is reachable.")
        return 2
    if set_proxy(new):
        log(f"[heal] TELEGRAM_PROXY -> {new}; restarting gateway")
        _mark_restart()
        restart_gateway()
    else:
        log(f"[heal] proxy unchanged ({new}); no restart")
    return 0


def check() -> int:
    cur = current_proxy()
    if direct_telegram_ok():
        return 0 if not cur else 1  # if direct works but a (maybe stale) proxy is set, suggest heal
    if cur and proxy_reaches_telegram(cur):
        silent = telegram_silent_minutes()
        if silent is not None and silent >= MAX_SILENT_MIN and gateway_running():
            return 1  # reachable but wedged
        return 0
    return 1


def status() -> int:
    cur = current_proxy()
    direct = direct_telegram_ok()
    print(f"direct api.telegram.org reachable : {'yes' if direct else 'no (ISP block)'}")
    print(f"TELEGRAM_PROXY in {ENV_FILE}     : {cur or '(unset)'}")
    proxy_ok = False
    if cur:
        lat = proxy_latency(cur)
        proxy_ok = lat is not None
        cc = _exit_country(cur) if proxy_ok else None
        if proxy_ok:
            print(f"current proxy reaches Bot API     : yes  ({lat:.2f}s{f', exit {cc}' if cc else ''})")
        else:
            print("current proxy reaches Bot API     : NO")
    silent = telegram_silent_minutes()
    if silent is not None:
        flag = "  ⚠ WEDGED?" if silent >= MAX_SILENT_MIN else ""
        print(f"telegram log quiet for            : {silent:.1f} min (wedge threshold {MAX_SILENT_MIN}m){flag}")
    print(f"gateway running                   : {'yes' if gateway_running() else 'NO'}")
    healthy = (direct and not cur) or (cur and proxy_ok)
    if healthy and silent is not None and silent >= MAX_SILENT_MIN and gateway_running():
        healthy = False  # reachable proxy but wedged poll = not healthy
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
