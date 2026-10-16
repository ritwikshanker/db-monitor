#!/usr/bin/env python3
"""
db_lastminute_monitor.py — log bahn.de prices for a set of weekly travel scenarios.

Each *scenario* = route (origin -> destination, optional via, exact number of changes),
a weekday, and a departure/arrival time window. For every run the script resolves the
scenarios onto the dates of the *following* week (the week the Last-Minute Super Sparpreis
is sold for on Sat/Sun), fetches each (route, date) once, applies every scenario's window,
and appends one CSV row per matching connection. A cron job running Fri evening -> Sun
then tells you: does a cheap fare appear, when, at what price, how fast it goes.

Built-in scenarios (edit with --write-config / --config scenarios.json):
  R-out-Thu   Aachen Hbf -> Göttingen   Thu  dep >= 15:30
  R-out-Fri   Aachen Hbf -> Göttingen   Fri  dep >= 13:30
  R-back-Mon  Göttingen  -> Aachen Hbf  Mon  dep <= 13:00, arr <= 20:00
  R-back-Sun  Göttingen  -> Aachen Hbf  Sun  dep >= 16:00 (last trains)
  P-out-Fri   Göttingen  -> Aachen Hbf  Fri  dep >= 14:00 (after half a workday)
  P-back-Sun  Aachen Hbf -> Göttingen   Sun  dep >= 16:00 (last trains)
  P-back-Mon  Aachen Hbf -> Göttingen   Mon  dep <= 08:00 (first trains)
All: exactly 1 change at Frankfurt(Main)Hbf, ICE/EC/IC legs only, 2nd class, 1 adult, BahnCard 25.

Uses the same unofficial JSON endpoints the bahn.de website calls (no login needed).
Python >= 3.9. Transports (--transport, default auto = first available):
  browser   Playwright driving the installed Google Chrome (headless; Playwright's Chromium as
            fallback): loads the bahn.de search page once, lets the site's own JavaScript and some
            pointer activity satisfy Akamai's bot protection, then issues every API call as a
            same-origin fetch() from inside that page.  pip install playwright &&
            python3 -m playwright install chromium   (the only transport whose POSTs get through)
  curl_cffi Chrome TLS impersonation (pip install curl_cffi) — GET passes, POSTs are 403 OPS_BLOCKED
  urllib    plain stdlib — everything is 403 OPS_BLOCKED; kept for completeness/tests

Examples
  python3 db_lastminute_monitor.py                        # following week, append to db_prices.csv
  python3 db_lastminute_monitor.py --weeks 1,2,3          # also scan the two weeks after (regular Sparpreis)
  python3 db_lastminute_monitor.py --dates 2026-10-22     # specific day(s) only; scenarios still filter by weekday
  python3 db_lastminute_monitor.py --write-config scenarios.json   # dump defaults, edit, then --config scenarios.json
  python3 db_lastminute_monitor.py --threshold 20 --ntfy my-secret-topic --macos-notify

Exit codes: 0 ok, 10 = at least one connection at/below the threshold,
            2 = network/API failure or bahn.de rate limit (403 OPS_BLOCKED; run aborted, no retry).

Request budget per run (defaults): 3 station lookups + 7 day queries + <= 7 offer lookups,
~4 s apart (~1 min). bahn.de blocks clients that burst (~15 requests in 10 s were enough);
hourly runs are fine.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # very old Python / no tzdata
    BERLIN = None

# Optional third-party dependency: curl_cffi (pip install curl_cffi). bahn.de's bot protection
# rejects plain urllib/curl with 403 OPS_BLOCKED (TLS fingerprint); curl_cffi impersonates a
# real Chrome handshake and keeps the Akamai cookies in a session. Falls back to urllib if absent.
if sys.platform == "darwin":
    # conda Pythons on macOS: curl_cffi's bundled libcurl needs CoreFoundation already loaded,
    # otherwise dlopen fails with "symbol not found in flat namespace '_CFArrayAppendValue'".
    try:
        import ctypes
        ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError:
        pass
try:
    from curl_cffi import requests as _cffi_requests
    HAVE_CFFI = True
except ImportError:
    _cffi_requests = None
    HAVE_CFFI = False

# Optional third-party dependency: playwright (pip install playwright; python3 -m playwright install chromium).
# Akamai's protection on the POST endpoints wants its `_abck` sensor cookie validated by JavaScript
# running in a real browser, which curl_cffi cannot provide (verified 16 Oct 2026: GET passes, every
# POST is 403 OPS_BLOCKED). The browser transport runs the site's JS once and fetches from inside it.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    _sync_playwright = None
    HAVE_PLAYWRIGHT = False

SITE = "https://www.bahn.de"
BASE = SITE + "/web/api"
SEARCH_PAGE = SITE + "/buchung/fahrplan/suche"

TRANSPORTS = ("auto", "browser", "curl_cffi", "urllib")
TRANSPORT = "auto"              # --transport; resolved by resolve_transport()
BROWSER_HEADLESS = True         # --headed flips this
BROWSER_PROFILE: str | None = None     # --browser-profile DIR -> launch_persistent_context
BROWSER_EXECUTABLE: str | None = None  # --browser-executable PATH -> system chromium (e.g. on the Pi)
BROWSER_CHANNEL = "auto"        # --browser-channel: chrome (installed Google Chrome) | chromium (Playwright's) | msedge
BROWSER_SETTLE = 20.0           # seconds of pointer activity after load before the first call (sensor validation)
BROWSER_BLOCK_RETRIES = 4       # browser transport only: on OPS_BLOCKED, do more page activity and retry up to N times
BROWSER_RETRY_ACTIVITY = 10.0   # seconds of activity before each such retry
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Origin": "https://www.bahn.de",
    "Referer": "https://www.bahn.de/buchung/fahrplan/suche",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
}
PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]
LONG_DISTANCE = {"ICE", "EC_IC"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CSV_FIELDS = [
    "run_time", "scenario", "route", "travel_date", "weekday", "days_ahead", "dep", "arr",
    "duration_min", "trains", "change_station", "changes", "products", "price_eur",
    "list_price_eur", "offer_name", "offer_labels", "below_threshold", "trip_id",
]

DEFAULT_CONFIG = {
    "traveller": {"bahncard": "BAHNCARD25", "klasse": "KLASSE_2"},
    "defaults": {"via": "Frankfurt(Main)Hbf", "changes": 1, "long_distance_only": True, "threshold_eur": 20.0},
    "scenarios": [
        {"name": "R-out-Thu", "from": "Aachen Hbf", "to": "Göttingen", "weekday": "Thu", "earliest_dep": "15:30"},
        {"name": "R-out-Fri", "from": "Aachen Hbf", "to": "Göttingen", "weekday": "Fri", "earliest_dep": "13:30"},
        {"name": "R-back-Mon", "from": "Göttingen", "to": "Aachen Hbf", "weekday": "Mon",
         "latest_dep": "13:00", "latest_arr": "20:00"},
        {"name": "R-back-Sun", "from": "Göttingen", "to": "Aachen Hbf", "weekday": "Sun", "earliest_dep": "16:00"},
        {"name": "P-out-Fri", "from": "Göttingen", "to": "Aachen Hbf", "weekday": "Fri", "earliest_dep": "14:00"},
        {"name": "P-back-Sun", "from": "Aachen Hbf", "to": "Göttingen", "weekday": "Sun", "earliest_dep": "16:00"},
        {"name": "P-back-Mon", "from": "Aachen Hbf", "to": "Göttingen", "weekday": "Mon", "latest_dep": "08:00"},
    ],
}


# ----------------------------------------------------------------------------- HTTP

class RateLimited(RuntimeError):
    """bahn.de answered 403 OPS_BLOCKED — abort the whole run, never retry."""


_SESSION = None


def _session():
    """curl_cffi session impersonating Chrome, warmed up once on the homepage so Akamai's
    cookies exist before the first API call."""
    global _SESSION
    if _SESSION is None:
        s = _cffi_requests.Session(impersonate="chrome")
        try:
            s.get("https://www.bahn.de/", timeout=30)
        except Exception as e:  # warm-up is best effort
            print(f"[warn] homepage warm-up failed: {e}", file=sys.stderr)
        _SESSION = s
    return _SESSION


class Browser:
    """Chromium driven by Playwright. Loads the bahn.de search page once (the site's own JS then
    obtains/validates Akamai's `_abck` cookie) and afterwards runs every API call as a same-origin
    `fetch()` inside that page, so cookies, TLS and JS fingerprint are all the real browser's.
    Relative fetch URLs keep working if bahn.de redirects www -> int.bahn.de for a non-DE IP."""

    # The website also sends an x-correlation-id header; it is not required and the one POST that got
    # through during testing (16 Oct 2026) was without it, so it is deliberately left out.
    _JS = """async ({method, path, body}) => {
        const headers = {"Accept": "application/json",
                         "Content-Type": "application/json; charset=utf-8"};
        const r = await fetch("/web/api" + path, {method, headers, credentials: "same-origin",
                                                   body: body === null ? undefined : body});
        return {status: r.status, text: await r.text()};
    }"""

    def __init__(self, headless: bool = True, profile_dir: str | None = None, executable: str | None = None,
                 channel: str = "auto", settle: float = 20.0):
        self.headless, self.profile_dir, self.executable = headless, profile_dir, executable
        self.channel, self.settle = channel, settle
        self.used_channel = ""
        self._pw = self._browser = self._ctx = self.page = None
        self._moves = 0

    def _launch(self, launch: dict, context: dict):
        """Launch with the requested channel; 'auto' = installed Google Chrome, else Playwright's Chromium.
        Real Chrome is preferred: in testing (16 Oct 2026) bahn.de refused every POST from Playwright's
        Chromium build (client-hint brand "Chromium" only), while Chrome passed after the warm-up."""
        channels = ["chrome", "chromium"] if self.channel == "auto" else [self.channel]
        last = None
        for ch in channels:
            kw = dict(launch)
            if self.executable:
                kw["executable_path"] = self.executable
            else:
                kw["channel"] = ch      # "chromium" = full Chromium in new-headless mode, not headless_shell
            try:
                if self.profile_dir:
                    ctx = self._pw.chromium.launch_persistent_context(self.profile_dir, **kw, **context)
                    self.used_channel = ch
                    return None, ctx
                browser = self._pw.chromium.launch(**kw)
                self.used_channel = ch
                return browser, None
            except Exception as e:  # channel not installed
                last = e
                if self.executable:
                    break
        raise RuntimeError(f"could not launch a browser ({self.channel}): {last}")

    def start(self) -> None:
        self._pw = _sync_playwright().start()
        launch = {"headless": self.headless, "args": ["--disable-blink-features=AutomationControlled"]}
        context = {"locale": "de-DE", "timezone_id": "Europe/Berlin", "viewport": {"width": 1280, "height": 900}}
        self._browser, self._ctx = self._launch(launch, context)
        if self._browser is not None:
            if self.headless:
                # new-headless Chrome/Chromium still announces "HeadlessChrome/156…"; present the plain Chrome UA
                probe = self._browser.new_context()
                ua = probe.new_page().evaluate("navigator.userAgent")
                probe.close()
                context["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
            self._ctx = self._browser.new_context(**context)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if self.headless and self.profile_dir:
            ua = self.page.evaluate("navigator.userAgent")
            if "HeadlessChrome" in ua:  # persistent context: no probe possible before launch -> per-page CDP override
                self._ctx.new_cdp_session(self.page).send(
                    "Network.setUserAgentOverride", {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
        self.page.goto(SEARCH_PAGE, wait_until="load", timeout=60_000)
        # Let the page's scripts (incl. the bot-protection sensor) run and see some pointer activity,
        # as they would from a real visitor, before the first API call.
        self.activity(self.settle)

    def activity(self, seconds: float) -> None:
        """Pointer movement and the odd scroll for about `seconds`, with irregular paths, speeds and
        pauses: the bot-protection sensor scores the telemetry it collects from user events and
        re-posts it as events accumulate. Verified 16 Oct 2026 (real Chrome): POSTs were refused for
        the first ~45 s of page life and accepted after more activity. No clicks (nothing navigates)."""
        rnd = random.Random()
        deadline = time.monotonic() + seconds
        x, y = 640, 450
        while time.monotonic() < deadline:
            self._moves += 1
            tx, ty = rnd.randint(80, 1200), rnd.randint(120, 820)
            # two-segment path via a control point -> curved, with variable step count/speed
            cx, cy = (x + tx) / 2 + rnd.randint(-200, 200), (y + ty) / 2 + rnd.randint(-150, 150)
            for px, py in ((cx, cy), (tx, ty)):
                self.page.mouse.move(px, py, steps=rnd.randint(6, 25))
                self.page.wait_for_timeout(rnd.randint(30, 250))
            x, y = tx, ty
            if rnd.random() < 0.25:
                self.page.mouse.wheel(0, rnd.choice((-120, 120, 240)))
            self.page.wait_for_timeout(rnd.randint(250, 1400))

    def fetch(self, method: str, path: str, payload: dict | None) -> tuple[int, str]:
        if self.page is None:
            self.start()
        else:
            self.activity(1.5)
        body = json.dumps(payload) if payload is not None else None
        r = self.page.evaluate(self._JS, {"method": method, "path": path, "body": body})
        return int(r["status"]), r["text"]

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self.page = None


_BROWSER: Browser | None = None


def _browser() -> Browser:
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = Browser(BROWSER_HEADLESS, BROWSER_PROFILE, BROWSER_EXECUTABLE, BROWSER_CHANNEL, BROWSER_SETTLE)
    return _BROWSER


def close_browser() -> None:
    global _BROWSER
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None


def resolve_transport(name: str | None = None) -> str:
    name = name or TRANSPORT
    if name == "auto":
        return "browser" if HAVE_PLAYWRIGHT else "curl_cffi" if HAVE_CFFI else "urllib"
    if name == "browser" and not HAVE_PLAYWRIGHT:
        raise SystemExit("--transport browser needs: pip install playwright && python3 -m playwright install chromium")
    if name == "curl_cffi" and not HAVE_CFFI:
        raise SystemExit("--transport curl_cffi needs: pip install curl_cffi")
    if name not in TRANSPORTS:
        raise SystemExit(f"unknown transport {name!r}; one of {TRANSPORTS}")
    return name


def chrome_installed() -> bool:
    """Where Playwright looks for channel="chrome" (Google Chrome stable)."""
    paths = {
        "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
        "linux": ["/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"],
        "win32": [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")],
    }.get(sys.platform, [])
    return any(os.path.exists(c) for c in paths)


def describe_transport(name: str) -> str:
    if name == "browser":
        which = getattr(_BROWSER, "used_channel", "") or BROWSER_CHANNEL
        if which == "auto":
            which = "chrome" if chrome_installed() else "chromium"
        return ("browser (Playwright, " + which + ", " + ("headed" if not BROWSER_HEADLESS else "headless")
                + (", persistent profile" if BROWSER_PROFILE else "") + ")")
    if name == "curl_cffi":
        return "curl_cffi (Chrome impersonation)"
    return "urllib"


def _blocked(status: int, body: str, path: str, transport: str) -> None:
    if status == 403 and "OPS_BLOCKED" in body:
        # Either bahn.de's rate limiter (~15 requests in 10 s from one client) or its bot
        # protection rejecting the client. Never retry inside a run.
        hint = {
            "urllib": " — plain urllib is always rejected; use --transport browser (pip install playwright)",
            "curl_cffi": " — Akamai rejects POSTs from TLS impersonation; use --transport browser (pip install playwright)",
            "browser": (f" — still refused after {BROWSER_BLOCK_RETRIES} activity retries: burst rate limit / penalty box "
                        "(wait 10 min)" + (", or try --headed and --browser-channel chrome" if BROWSER_HEADLESS
                                           else ", or try --browser-channel chrome / --browser-profile DIR")),
        }.get(transport, "")
        raise RateLimited(f"bahn.de blocked this client (HTTP 403 OPS_BLOCKED) on {path}{hint}")


def _handle_http_status(status: int, body: str, path: str, transport: str, attempt: int, retries: int):
    """Returns the parsed JSON on success, None when the caller should retry, raises otherwise."""
    if status >= 400:
        _blocked(status, body[:300], path, transport)
        if status in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * attempt)
            return None
        raise RuntimeError(f"HTTP {status} on {path}: {body[:300]}")
    return json.loads(body) if body.strip() else {}


def http_json(method: str, path: str, payload: dict | None = None, retries: int = 3, timeout: int = 30):
    """Single choke point for every bahn.de API call (tests mock this). Dispatches on the transport."""
    transport = resolve_transport()
    url = BASE + path
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if transport == "browser":
                for block_try in range(BROWSER_BLOCK_RETRIES + 1):
                    status, body = _browser().fetch(method, path, payload)
                    if not (status == 403 and "OPS_BLOCKED" in body) or block_try == BROWSER_BLOCK_RETRIES:
                        break
                    # Akamai refuses POSTs until the page's sensor has seen enough visitor activity:
                    # keep the page alive, add activity, try again (bounded; a burst would be worse).
                    print(f"[info] OPS_BLOCKED on {path}: more page activity, retry "
                          f"{block_try + 1}/{BROWSER_BLOCK_RETRIES}", file=sys.stderr)
                    _browser().activity(BROWSER_RETRY_ACTIVITY)
                out = _handle_http_status(status, body, path, transport, attempt, retries)
                if out is None:
                    continue
                return out
            if transport == "curl_cffi":
                headers = {k: v for k, v in HEADERS.items() if k != "User-Agent"}  # impersonation sets the UA
                r = _session().request(method, url, json=payload, headers=headers, timeout=timeout)
                out = _handle_http_status(r.status_code, r.text, path, transport, attempt, retries)
                if out is None:
                    continue
                return out
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if body.strip() else {}
        except (RateLimited, RuntimeError):
            raise
        except urllib.error.HTTPError as e:
            snippet = e.read()[:300].decode("utf-8", "replace")
            _blocked(e.code, snippet, path, transport)
            last_err = RuntimeError(f"HTTP {e.code} on {path}: {snippet}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise
        except Exception as e:
            # Playwright errors (page navigated away, context closed, fetch TypeError, ...):
            # restart the browser and retry, then give up on this request.
            if transport != "browser":
                raise
            last_err = e
            if attempt < retries:
                print(f"[warn] browser call failed ({type(e).__name__}: {str(e)[:120]}); restarting browser", file=sys.stderr)
                close_browser()
                time.sleep(2 * attempt)
                continue
            raise
    raise last_err  # pragma: no cover


# ----------------------------------------------------------------------------- stations

def normalise_station_id(raw_id: str) -> str:
    """Rebuild the HAFAS location id the way the bahn.de UI sends it:
    A=1@O=<name>@X=..@Y=..@U=..@L=<eva>@B=1@p=<ts>@  (no trailing i=... segment)."""
    parts = {}
    for seg in raw_id.split("@"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k] = v
    order = ["A", "O", "X", "Y", "U", "L"]
    out = [f"{k}={parts[k]}" for k in order if k in parts]
    out.append("B=1")
    if "p" in parts:
        out.append(f"p={parts['p']}")
    return "@".join(out) + "@"


class Stations:
    """Resolve + cache station names -> (id, canonical name); one API call per distinct name."""

    def __init__(self, pause: float):
        self.pause = pause
        self.cache: dict[str, tuple[str, str]] = {}

    def get(self, query: str) -> tuple[str, str]:
        key = query.strip().lower()
        if key not in self.cache:
            if self.cache:
                time.sleep(self.pause)
            q = urllib.parse.quote(query)
            hits = http_json("GET", f"/reiseloesung/orte?suchbegriff={q}&typ=ALL&limit=10")
            if not hits:
                raise SystemExit(f"No station found for {query!r}")
            stations = [h for h in hits if h.get("type") == "ST"] or hits
            exact = [h for h in stations if h.get("name", "").lower() == key]
            hit = (exact or stations)[0]
            self.cache[key] = (normalise_station_id(hit["id"]), hit["name"])
        return self.cache[key]


# ----------------------------------------------------------------------------- schedule gate

# Run slots in Europe/Berlin time, as (weekday, hour, minute-rounded-to-15). Used by --when-scheduled:
# cron fires the script every 15 min in whatever timezone the machine has; the script decides.
# Fri 20-23 hourly (baseline before release), Sat 00:00/00:15/00:30/00:45 (release ~00:01?),
# Sat 01-12 hourly + 18:00 (sell-out), Sun 09:00 + 18:00.
RUN_SLOTS = (
    {(4, h, 0) for h in range(20, 24)}
    | {(5, 0, m) for m in (0, 15, 30, 45)}
    | {(5, h, 0) for h in range(1, 13)}
    | {(5, 18, 0)}
    | {(6, 9, 0), (6, 18, 0)}
)


def scheduled_now(now_berlin: datetime) -> bool:
    return (now_berlin.weekday(), now_berlin.hour, (now_berlin.minute // 15) * 15) in RUN_SLOTS


def describe_schedule(now_berlin: datetime, now_local: datetime) -> str:
    lines = [f"Berlin now: {now_berlin:%a %Y-%m-%d %H:%M %Z}   machine now: {now_local:%a %Y-%m-%d %H:%M %Z}",
             f"would run now: {'YES' if scheduled_now(now_berlin) else 'no'}",
             "upcoming slots (Berlin -> machine time):"]
    probe = now_berlin.replace(minute=(now_berlin.minute // 15) * 15, second=0, microsecond=0)
    shown = 0
    while shown < 8:
        probe += timedelta(minutes=15)
        if scheduled_now(probe):
            local = probe.astimezone(now_local.tzinfo) if now_local.tzinfo else probe
            lines.append(f"  {probe:%a %H:%M} -> {local:%a %H:%M}")
            shown += 1
    return "\n".join(lines)


# ----------------------------------------------------------------------------- dates / scenarios

def following_week(today: date, offset_weeks: int = 1) -> list[date]:
    """Mon-Sun of the week after the coming weekend (DB's 'Folgewoche' for Last-Minute fares).
    Fri 16 Oct / Sat 17 Oct / Sun 18 Oct -> Mon 19 .. Sun 25 Oct. offset_weeks=2 -> the week after."""
    next_monday = today + timedelta(days=7 - today.weekday()) + timedelta(weeks=offset_weeks - 1)
    return [next_monday + timedelta(days=i) for i in range(7)]


def to_minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k in ("traveller", "defaults"):
            cfg[k].update(user.get(k, {}))
        if "scenarios" in user:
            cfg["scenarios"] = user["scenarios"]
    d = cfg["defaults"]
    for s in cfg["scenarios"]:
        s.setdefault("via", d["via"])
        s.setdefault("changes", d["changes"])
        s.setdefault("long_distance_only", d["long_distance_only"])
        s.setdefault("threshold_eur", d["threshold_eur"])
        if s.get("weekday") not in WEEKDAYS and s.get("weekday") is not None:
            raise SystemExit(f"scenario {s.get('name')}: weekday must be one of {WEEKDAYS}")
    return cfg


# ----------------------------------------------------------------------------- search

def search_body(from_id: str, to_id: str, via_id: str | None, day: date, reisende: list, klasse: str) -> dict:
    return {
        "abfahrtsHalt": from_id,
        "anfrageZeitpunkt": f"{day.isoformat()}T00:00:00",
        "ankunftsHalt": to_id,
        "ankunftSuche": "ABFAHRT",
        "klasse": klasse,
        "produktgattungen": PRODUCTS,
        "reisende": reisende,
        "schnelleVerbindungen": True,
        "zwischenhalte": [{"id": via_id}] if via_id else [],
        "autonomeReservierungOnly": False,
        "bikeCarriage": False,
        "reservierungsKontingenteVorhanden": False,
        "nurFahrbareVerbindungen": False,
        "nurDeutschlandTicketVerbindungen": False,
        "deutschlandTicketVorhanden": False,
    }


def real_legs(verbindung: dict) -> list[dict]:
    """Train legs only (drop walking transfers such as Frankfurt Hbf -> Hbf (tief))."""
    legs = []
    for leg in verbindung.get("verbindungsAbschnitte", []):
        vm = leg.get("verkehrsmittel") or {}
        if not vm.get("name") or vm.get("produktGattung") in (None, "WALK") or vm.get("typ") == "WALK":
            continue
        legs.append(leg)
    return legs


def minutes_since_midnight(ts: str | None, day: date) -> int | None:
    """Minutes since 00:00 of `day` (can exceed 1440 for arrivals after midnight)."""
    if not ts or len(ts) < 16:
        return None
    try:
        d = date.fromisoformat(ts[:10])
    except ValueError:
        return None
    return (d - day).days * 1440 + int(ts[11:13]) * 60 + int(ts[14:16])


def fmt_min(m: int | None) -> str:
    if m is None:
        return ""
    return f"{(m // 60) % 24:02d}:{m % 60:02d}" + ("+1" if m >= 1440 else "")


def parse_day(resp: dict, day: date) -> list[dict]:
    rows, seen = [], set()
    for interval in resp.get("intervalle", []):
        for c in interval.get("verbindungen", []):
            v = c.get("verbindung") or {}
            trip_id = v.get("tripId")
            if trip_id in seen:
                continue
            seen.add(trip_id)
            legs = real_legs(v)
            if not legs:
                continue
            dep_m = minutes_since_midnight((legs[0].get("abfahrt") or {}).get("sollzeit"), day)
            arr_m = minutes_since_midnight((legs[-1].get("ankunft") or {}).get("sollzeit"), day)
            rows.append({
                "travel_date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()],
                "dep": fmt_min(dep_m), "arr": fmt_min(arr_m),
                "_dep_m": dep_m, "_arr_m": arr_m,
                "duration_min": round((v.get("verbindungsDauerInSeconds") or 0) / 60),
                "trains": " + ".join(" ".join(l["verkehrsmittel"]["name"].split()) for l in legs),
                "change_station": legs[0].get("ankunftsOrt", "") if len(legs) > 1 else "",
                "changes": v.get("umstiegsAnzahl"),
                "products": ",".join(sorted({l["verkehrsmittel"].get("produktGattung", "?") for l in legs})),
                "price_eur": (c.get("abPreis") or {}).get("betrag"),
                "list_price_eur": None,
                "offer_name": "",
                "offer_labels": "",
                "trip_id": trip_id,
                "_ctx": v.get("ctxRecon"),
            })
    return rows


def matches(row: dict, sc: dict) -> bool:
    if row["changes"] != sc["changes"]:
        return False
    via = sc.get("via") or ""
    if sc["changes"] >= 1 and via:
        prefix = via.split("(")[0].lower()          # "Frankfurt(Main)Hbf" -> "frankfurt"
        if not row["change_station"].lower().startswith(prefix):
            return False
    if sc.get("long_distance_only", True) and not set(row["products"].split(",")) <= LONG_DISTANCE:
        return False
    dep, arr = row["_dep_m"], row["_arr_m"]
    lo, hi, la = to_minutes(sc.get("earliest_dep")), to_minutes(sc.get("latest_dep")), to_minutes(sc.get("latest_arr"))
    if lo is not None and (dep is None or dep < lo):
        return False
    if hi is not None and (dep is None or dep > hi):
        return False
    if la is not None and (arr is None or arr > la):
        return False
    return True


# ----------------------------------------------------------------------------- offers

def fetch_offers(ctx_recon: str, reisende: list, klasse: str) -> tuple[str, float | None, float | None, str]:
    """Return (offer_name, price, list_price, labels) of the cheapest offer in the booked class.
    The Last-Minute fare shows up here either by name or by an extra condition label ('Aktion')."""
    body = {
        "autonomeReservierung": False,
        "einstiegsTypList": ["STANDARD"],
        "klasse": klasse,
        "reisende": reisende,
        "reservierungsKontingenteVorhanden": False,
        "ctxRecon": ctx_recon,
        "deutschlandTicketVorhanden": False,
        "nurDeutschlandTicketVerbindungen": False,
    }
    resp = http_json("POST", "/angebote/recon", body)
    best = None
    for v in resp.get("verbindungen", []):
        for ra in v.get("reiseAngebote", []):
            if ra.get("klasse") != klasse or ra.get("typ") not in (None, "REISEANGEBOT"):
                continue
            price = (ra.get("preis") or {}).get("betrag")
            if price is None:
                continue
            if best is None or price < best[1]:
                labels = []
                for k in ra.get("konditionsAnzeigen", []) or []:
                    labels.append(k.get("kategorie") or k.get("anzeigeUeberschrift") or "")
                for fa in (ra.get("hinfahrt") or {}).get("fahrtAngebote", []) or []:
                    for k in fa.get("konditionsAnzeigen", []) or []:
                        lab = k.get("kategorie") or k.get("anzeigeUeberschrift") or ""
                        if lab and lab not in labels:
                            labels.append(lab)
                strike = ((ra.get("preisInfo") or {}).get("streichpreis") or {}).get("betrag")
                best = (ra.get("name", ""), price, strike, "|".join(x for x in labels if x))
    return best or ("", None, None, "")


# ----------------------------------------------------------------------------- notify

def notify(title: str, text: str, ntfy_topic: str | None, macos: bool) -> None:
    if ntfy_topic:
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{ntfy_topic}", data=text.encode("utf-8"),
                                         headers={"Title": title, "Priority": "high", "Tags": "train"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:  # notification must never break the run
            print(f"[warn] ntfy failed: {e}", file=sys.stderr)
    if macos:
        try:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display notification "{esc(text[:200])}" with title "{esc(title)}" sound name "Glass"'],
                           check=False, timeout=10)
        except Exception as e:
            print(f"[warn] macOS notification failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="scenarios JSON (see --write-config); default = built-in scenarios")
    ap.add_argument("--write-config", metavar="PATH", help="write the built-in config to PATH and exit")
    ap.add_argument("--only", help="comma-separated scenario names to run (default all)")
    ap.add_argument("--weeks", default="1",
                    help="which weeks to scan, relative to the coming weekend: 1 = following week (Last-Minute slot), "
                         "'1,2,3' adds the weeks after (regular Sparpreis watch)")
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD; overrides --weeks (scenarios still filter by weekday)")
    ap.add_argument("--threshold", type=float, help="EUR; overrides every scenario's threshold_eur")
    ap.add_argument("--no-offers", action="store_true", help="skip offer-name lookup (fewer requests)")
    ap.add_argument("--offers-below", type=float, default=30.0,
                    help="look up offer names for connections at/below this price (cheapest per scenario+day is always looked up)")
    ap.add_argument("--out", default="db_prices.csv", help="CSV file to append to")
    ap.add_argument("--ntfy", metavar="TOPIC", help="push an alert to https://ntfy.sh/TOPIC when a threshold is hit")
    ap.add_argument("--macos-notify", action="store_true", help="macOS notification when a threshold is hit")
    ap.add_argument("--sleep", type=float, default=4.0,
                    help="pause between requests, seconds. Keep >= 3: bahn.de blocks bursts (403 OPS_BLOCKED)")
    ap.add_argument("--transport", choices=TRANSPORTS, default="auto",
                    help="browser = Playwright Chromium (the only one whose POSTs pass bahn.de's bot protection), "
                         "curl_cffi, urllib; auto = first available in that order")
    ap.add_argument("--headed", action="store_true", help="browser transport: show the Chromium window (headless=False)")
    ap.add_argument("--browser-profile", metavar="DIR",
                    help="browser transport: persistent Chromium profile directory (keeps cookies between runs)")
    ap.add_argument("--browser-executable", metavar="PATH",
                    help="browser transport: use this Chromium/Chrome binary instead of Playwright's download")
    ap.add_argument("--browser-channel", choices=("auto", "chrome", "chromium", "msedge"), default="auto",
                    help="browser transport: auto = installed Google Chrome, else Playwright's Chromium "
                         "(bahn.de blocked every POST from the Chromium build in testing)")
    ap.add_argument("--when-scheduled", action="store_true",
                    help="only run if the current Europe/Berlin time is one of the built-in weekend slots; "
                         "pair with a '*/15 * * * *' cron line on a machine in any timezone")
    ap.add_argument("--show-schedule", action="store_true", help="print the next run slots in Berlin and machine time, exit")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    global TRANSPORT, BROWSER_HEADLESS, BROWSER_PROFILE, BROWSER_EXECUTABLE, BROWSER_CHANNEL
    TRANSPORT = args.transport
    BROWSER_HEADLESS = not args.headed
    BROWSER_PROFILE = args.browser_profile
    BROWSER_EXECUTABLE = args.browser_executable
    BROWSER_CHANNEL = args.browser_channel
    try:
        return run(args)
    finally:
        close_browser()


def run(args) -> int:
    if args.show_schedule or args.when_scheduled:
        if BERLIN is None:
            print("[error] zoneinfo/Europe/Berlin unavailable on this Python; cannot gate on Berlin time", file=sys.stderr)
            return 2
        now_b = datetime.now(BERLIN)
        if args.show_schedule:
            print(describe_schedule(now_b, datetime.now().astimezone()))
            return 0
        if not scheduled_now(now_b):
            return 0  # silent: not a slot

    if args.write_config:
        with open(args.write_config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.write_config}")
        return 0

    cfg = load_config(args.config)
    scenarios = cfg["scenarios"]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s["name"] in wanted]
        if not scenarios:
            raise SystemExit(f"--only matched nothing; known: {[s['name'] for s in cfg['scenarios']]}")
    if args.threshold is not None:
        for s in scenarios:
            s["threshold_eur"] = args.threshold

    tr = cfg["traveller"]
    klasse = tr.get("klasse", "KLASSE_2")
    bahncard = tr.get("bahncard", "KEINE_ERMAESSIGUNG")
    erm_klasse = "KLASSENLOS" if bahncard == "KEINE_ERMAESSIGUNG" else klasse
    reisende = [{"typ": "ERWACHSENER", "ermaessigungen": [{"art": bahncard, "klasse": erm_klasse}],
                 "alter": [], "anzahl": 1}]

    now = datetime.now(BERLIN) if BERLIN else datetime.now()
    run_time = now.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    today = now.date()

    if args.dates:
        days = sorted({date.fromisoformat(d.strip()) for d in args.dates.split(",") if d.strip()})
    else:
        days = []
        for w in args.weeks.split(","):
            days.extend(following_week(today, int(w)))
        days = sorted(set(days))

    # (origin, destination, via, date) -> list of scenarios that need it. One request per key.
    jobs: dict[tuple, list[dict]] = {}
    for sc in scenarios:
        for day in days:
            if sc.get("weekday") and WEEKDAYS[day.weekday()] != sc["weekday"]:
                continue
            jobs.setdefault((sc["from"], sc["to"], sc.get("via") or "", day), []).append(sc)
    if not jobs:
        print("nothing to do: no scenario weekday falls on the requested dates")
        return 0

    log = (lambda *a, **k: None) if args.quiet else print
    log(f"{run_time}  {len(jobs)} route-days for {len(scenarios)} scenarios | {bahncard} {klasse} | "
        f"dates {days[0]}..{days[-1]} | transport: {describe_transport(resolve_transport())}")

    stations = Stations(args.sleep)
    try:
        names = {n for key in jobs for n in key[:3] if n}
        for n in sorted(names):
            stations.get(n)
    except RateLimited as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[error] station lookup failed: {e}", file=sys.stderr)
        return 2

    all_rows: list[dict] = []
    failures, blocked = 0, False
    for key in sorted(jobs, key=lambda k: (k[3], k[0], k[1])):
        origin, dest, via, day = key
        from_id, from_name = stations.get(origin)
        to_id, to_name = stations.get(dest)
        via_id, via_name = stations.get(via) if via else (None, "")
        route = f"{from_name} -> {to_name}"
        time.sleep(args.sleep)
        try:
            resp = http_json("POST", "/angebote/tagesbestpreis",
                             search_body(from_id, to_id, via_id, day, reisende, klasse))
        except RateLimited as e:
            print(f"[error] {e} — aborting this run; the next scheduled run will retry.", file=sys.stderr)
            blocked = True
            break
        except Exception as e:
            failures += 1
            print(f"[error] {route} {day}: {e}", file=sys.stderr)
            continue
        day_rows = parse_day(resp, day)

        for sc in jobs[key]:
            sc_rows = [dict(r) for r in day_rows if matches(r, sc)]
            sc_rows.sort(key=lambda r: (r["price_eur"] is None, r["price_eur"] or 0))
            if not args.no_offers and not blocked:
                for i, r in enumerate(sc_rows):
                    p = r["price_eur"]
                    if (i == 0 or (p is not None and p <= args.offers_below)) and r.get("_ctx"):
                        time.sleep(args.sleep)
                        try:
                            name, price, strike, labels = fetch_offers(r["_ctx"], reisende, klasse)
                            r["offer_name"], r["list_price_eur"], r["offer_labels"] = name, strike, labels
                            if r["price_eur"] is None and price is not None:
                                r["price_eur"] = price
                        except RateLimited as e:
                            print(f"[warn] {e} — skipping remaining offer lookups this run.", file=sys.stderr)
                            blocked = True
                            break
                        except Exception as e:
                            print(f"[warn] offer lookup failed ({sc['name']} {day} {r['trains']}): {e}", file=sys.stderr)
            for r in sc_rows:
                r["run_time"] = run_time
                r["scenario"] = sc["name"]
                r["route"] = route
                r["days_ahead"] = (day - today).days
                r["below_threshold"] = r["price_eur"] is not None and r["price_eur"] <= sc["threshold_eur"]
            sc_rows.sort(key=lambda r: r["_dep_m"] or 0)
            all_rows.extend(sc_rows)

            prices = [r["price_eur"] for r in sc_rows if r["price_eur"] is not None]
            log(f"  {sc['name']:<11} {day} {WEEKDAYS[day.weekday()]} {route}: {len(sc_rows)} conn."
                + (f", {min(prices):.2f}-{max(prices):.2f} EUR" if prices else ", no prices"))
            for r in sc_rows:
                flag = "  <<< " if r["below_threshold"] else "      "
                log(f"{flag}{r['dep']}-{r['arr']} {r['trains']:<22} "
                    + (f"{r['price_eur']:>7.2f}" if r["price_eur"] is not None else "   n/a ")
                    + (f"  {r['offer_name']}" if r["offer_name"] else "")
                    + (f" [{r['offer_labels']}]" if r["offer_labels"] else ""))
        if blocked:
            break

    # ---- CSV
    if all_rows:
        new_file = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
        with open(args.out, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for r in all_rows:
                w.writerow(r)
        log(f"appended {len(all_rows)} rows to {args.out}")

    # ---- alerts
    hits = [r for r in all_rows if r["below_threshold"]]
    if hits:
        hits.sort(key=lambda r: r["price_eur"])
        lines = [f"{r['scenario']} {r['travel_date']} {r['dep']}-{r['arr']} {r['trains']} "
                 f"{r['price_eur']:.2f} EUR {r['offer_name']}".strip() for r in hits[:8]]
        text = "\n".join(lines)
        print("\nALERT — at/below threshold:\n" + text)
        notify(f"DB fare alert: {hits[0]['price_eur']:.2f} EUR ({hits[0]['scenario']})", text,
               args.ntfy, args.macos_notify)
        return 10
    if blocked or (failures and failures == len(jobs)):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
