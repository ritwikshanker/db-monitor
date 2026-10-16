import os as _os
SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "db_lastminute_monitor.py")
import importlib.util, json, sys, io, contextlib, os, csv, tempfile, urllib.error
from datetime import date

def load():
    spec = importlib.util.spec_from_file_location("m", SCRIPT)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    # never let a test reach the network: no transport dependency is "available" unless a test says so
    m.HAVE_PLAYWRIGHT = False; m.HAVE_CFFI = False
    return m

m = load()

# --- week maths
assert m.following_week(date(2026, 10, 16))[0] == date(2026, 10, 19)       # Fri -> Mon 19
assert m.following_week(date(2026, 10, 17))[0] == date(2026, 10, 19)       # Sat
assert m.following_week(date(2026, 10, 18))[-1] == date(2026, 10, 25)      # Sun -> ..Sun 25
assert m.following_week(date(2026, 10, 19))[0] == date(2026, 10, 26)       # Mon -> next week
assert m.following_week(date(2026, 10, 16), 2)[0] == date(2026, 10, 26)    # week after

# --- minutes / cross-midnight
assert m.minutes_since_midnight("2026-10-21T07:38:00", date(2026, 10, 21)) == 7 * 60 + 38
assert m.minutes_since_midnight("2026-10-22T00:45:00", date(2026, 10, 21)) == 1440 + 45
assert m.fmt_min(1440 + 45) == "00:45+1" and m.fmt_min(458) == "07:38"

# --- config
cfg = m.load_config(None)
assert len(cfg["scenarios"]) == 7 and all(s["via"] == "" and s["max_changes"] == 2 for s in cfg["scenarios"])

# --- fixtures
def leg(name, prod, von, nach, ab, an, day="2026-10-22", day_an=None):
    return {"verkehrsmittel": {"produktGattung": prod, "name": name}, "abfahrtsOrt": von, "ankunftsOrt": nach,
            "abfahrt": {"sollzeit": f"{day}T{ab}:00"}, "ankunft": {"sollzeit": f"{day_an or day}T{an}:00"}}
def conn(tid, price, umst, legs, dur_s=14000):
    return {"abPreis": {"betrag": price} if price is not None else None, "klasse": "KLASSE_2",
            "verbindung": {"tripId": tid, "ctxRecon": "ctx-" + tid, "umstiegsAnzahl": umst,
                           "verbindungsDauerInSeconds": dur_s, "verbindungsAbschnitte": legs}}

def out_day(day):  # Aachen -> Göttingen, the real every-2h pattern
    conns = []
    for i, (t, a, b) in enumerate([("07:38", "ICE 11", "ECE 8"), ("09:38", "ICE 13", "ICE 74"), ("11:38", "ICE 15", "ICE 6"),
                                   ("13:38", "ICE 315", "ICE 72"), ("15:38", "ICE 17", "ECE 4"), ("17:38", "ICE 317", "ICE 70")]):
        h = int(t[:2])
        conns.append(conn(f"o{day}{i}", 60 + i, 1, [leg(a, "ICE", "Aachen Hbf", "Frankfurt(Main)Hbf", t, f"{h+2:02d}:46", day),
                                                     leg(b, "ICE" if b.startswith("ICE") else "EC_IC", "Frankfurt(Main)Hbf", "Göttingen", f"{h+2:02d}:58", f"{h+4:02d}:31", day)]))
    # a late one arriving after midnight, and a Köln-change one
    conns.append(conn(f"o{day}late", 9.99, 1, [leg("ICE 319", "ICE", "Aachen Hbf", "Frankfurt(Main)Hbf", "21:38", "23:46", day),
                                               leg("ICE 1000", "ICE", "Frankfurt(Main)Hbf", "Göttingen", "23:58", "01:31", day, day_an=next_day(day))]))
    conns.append(conn(f"o{day}koeln", 5.0, 1, [leg("RE 1", "REGIONAL", "Aachen Hbf", "Köln Hbf", "16:00", "17:00", day),
                                               leg("ICE 9", "ICE", "Köln Hbf", "Göttingen", "17:10", "20:00", day)]))
    # diversion-style ones: 2 changes all-ICE via Köln + Hannover (kept), 3 changes all-ICE (dropped, despite 4 EUR)
    conns.append(conn(f"o{day}k2", 45.0, 2, [leg("ICE 9001", "ICE", "Aachen Hbf", "Köln Hbf", "16:21", "17:00", day),
                                             leg("ICE 9002", "ICE", "Köln Hbf", "Hannover Hbf", "17:15", "19:50", day),
                                             leg("ICE 9003", "ICE", "Hannover Hbf", "Göttingen", "20:10", "20:45", day)]))
    conns.append(conn(f"o{day}k3", 4.0, 3, [leg("ICE 9101", "ICE", "Aachen Hbf", "Köln Hbf", "16:50", "17:30", day),
                                            leg("ICE 9102", "ICE", "Köln Hbf", "Dortmund Hbf", "17:40", "18:50", day),
                                            leg("ICE 9103", "ICE", "Dortmund Hbf", "Hannover Hbf", "19:00", "20:40", day),
                                            leg("ICE 9104", "ICE", "Hannover Hbf", "Göttingen", "20:50", "21:25", day)]))
    # overnight routing, 2 changes all-ICE but 7h09 > max_duration_h (dropped, despite 3 EUR)
    conns.append(conn(f"o{day}night", 3.0, 2, [leg("ICE 319", "ICE", "Aachen Hbf", "Köln Hbf", "21:36", "22:15", day),
                                               leg("ICE 1272", "ICE", "Köln Hbf", "Hannover Hbf", "22:40", "01:30", day, day_an=next_day(day)),
                                               leg("IC 60470", "EC_IC", "Hannover Hbf", "Göttingen", "03:50", "04:45", next_day(day))],
                      dur_s=7 * 3600 + 9 * 60))
    return {"intervalle": [{"verbindungen": conns}]}

def back_day(day):  # Göttingen -> Aachen
    conns = []
    for i, t in enumerate(["06:27", "08:27", "10:27", "12:27", "14:27", "16:27", "18:27"]):
        h = int(t[:2])
        conns.append(conn(f"b{day}{i}", 40 + i, 1, [leg(f"ICE {100+i}", "ICE", "Göttingen", "Frankfurt(Main)Hbf", t, f"{h+2:02d}:05", day),
                                                     leg(f"ICE {10+i}", "ICE", "Frankfurt(Main)Hbf", "Aachen Hbf", f"{h+2:02d}:14", f"{h+4:02d}:22", day)]))
    return {"intervalle": [{"verbindungen": conns}]}

from datetime import timedelta
def next_day(d): return (date.fromisoformat(d) + timedelta(days=1)).isoformat()

calls = []
def fake_http(method, path, payload=None, **kw):
    calls.append(path)
    if path.startswith("/reiseloesung/orte"):
        import urllib.parse as up
        nm = up.unquote(path.split("suchbegriff=")[1].split("&")[0])
        return [{"type": "ST", "name": nm, "id": f"A=1@O={nm}@X=1@Y=2@U=80@L=800@p=99@i=xx@"}]
    if path == "/angebote/tagesbestpreis":
        day = payload["anfrageZeitpunkt"][:10]
        assert payload["zwischenhalte"] == []   # no via: diversions with other change stations must come back
        return out_day(day) if "Aachen" in payload["abfahrtsHalt"] else back_day(day)
    if path == "/angebote/recon":
        return {"verbindungen": [{"reiseAngebote": [{"name": "Super Sparpreis", "klasse": "KLASSE_2", "typ": "REISEANGEBOT",
                                                       "preis": {"betrag": 1}, "preisInfo": {"streichpreis": {"betrag": 2}},
                                                       "konditionsAnzeigen": [{"kategorie": "AKTION"}]}]}]}
    raise AssertionError(path)

m.http_json = fake_http
tmp = tempfile.mkdtemp(); out = os.path.join(tmp, "p.csv")
# pretend today is Fri 16 Oct 2026 -> following week 19..25 Oct
class FakeDT(m.datetime):
    @classmethod
    def now(cls, tz=None): return m.datetime(2026, 10, 16, 15, 0, tzinfo=tz)
m.datetime = FakeDT
sys.argv = ["x", "--out", out, "--sleep", "0"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = m.main()
printed = buf.getvalue()
print(printed)
assert rc == 10, rc   # the 9.99 late train on Thu/Fri? no: Thu/Fri scenarios have earliest_dep -> 21:38 late train qualifies (9.99 <= 20)

# request dedupe: 2 station lookups (no via), 7 route-days (Thu A->G, Fri A->G, Sun A->G, Mon A->G, Mon G->A, Sun G->A, Fri G->A)
assert calls.count("/angebote/tagesbestpreis") == 7, calls.count("/angebote/tagesbestpreis")
assert sum(1 for c in calls if c.startswith("/reiseloesung/orte")) == 2

with open(out) as f:
    rows = list(csv.DictReader(f))
by = {}
for r in rows: by.setdefault(r["scenario"], []).append(r)
assert set(by) == {"R-out-Thu", "R-out-Fri", "R-back-Mon", "R-back-Sun", "P-out-Fri", "P-back-Sun", "P-back-Mon"}, set(by)
# R-out-Thu: dep >= 15:30 -> 15:38, 16:21 (2 changes), 17:38, 21:38(late) ; RE-to-Köln one excluded (REGIONAL),
# 3-change one excluded (> max_changes)
assert [r["dep"] for r in by["R-out-Thu"]] == ["15:38", "16:21", "17:38", "21:38"], [r["dep"] for r in by["R-out-Thu"]]
assert by["R-out-Thu"][3]["arr"] == "01:31+1" and by["R-out-Thu"][3]["below_threshold"] == "True"
assert by["R-out-Thu"][1]["changes"] == "2" and by["R-out-Thu"][1]["change_station"] == "Köln Hbf / Hannover Hbf"
assert by["R-out-Thu"][0]["change_station"] == "Frankfurt(Main)Hbf"
assert all(r["travel_date"] == "2026-10-22" for r in by["R-out-Thu"])
# R-out-Fri: dep >= 13:30 -> 13:38 .. 21:38
assert [r["dep"] for r in by["R-out-Fri"]] == ["13:38", "15:38", "16:21", "17:38", "21:38"]
# R-back-Mon: dep <= 13:00 and arr <= 20:00 -> 06:27..12:27 (arr 16:22)
assert [r["dep"] for r in by["R-back-Mon"]] == ["06:27", "08:27", "10:27", "12:27"], [r["dep"] for r in by["R-back-Mon"]]
assert all(r["travel_date"] == "2026-10-19" and r["route"] == "Göttingen -> Aachen Hbf" for r in by["R-back-Mon"])
# R-back-Sun: dep >= 16:00 -> 16:27, 18:27
assert [r["dep"] for r in by["R-back-Sun"]] == ["16:27", "18:27"]
# P-out-Fri: dep >= 14:00 -> 14:27, 16:27, 18:27
assert [r["dep"] for r in by["P-out-Fri"]] == ["14:27", "16:27", "18:27"]
# P-back-Sun: Aachen->Göttingen dep >= 16:00 -> 16:21 (2 changes), 17:38, 21:38
assert [r["dep"] for r in by["P-back-Sun"]] == ["16:21", "17:38", "21:38"]
# P-back-Mon: dep <= 08:00 -> 07:38 only
assert [r["dep"] for r in by["P-back-Mon"]] == ["07:38"] and by["P-back-Mon"][0]["travel_date"] == "2026-10-19"
# offer lookup ran for cheapest per scenario-day (+ anything <= 30): labels captured
assert by["R-out-Thu"][3]["offer_labels"] == "AKTION" and by["R-out-Thu"][3]["list_price_eur"] == "2"
# an explicit via still restricts to connections changing there (any of the change stations)
k2 = {"changes": 2, "change_station": "Köln Hbf / Hannover Hbf", "products": "ICE", "_dep_m": 600, "_arr_m": 900}
assert m.matches(k2, {"via": "", "max_changes": 2}) and not m.matches(k2, {"via": "", "max_changes": 1})
assert m.matches(k2, {"via": "Hannover Hbf", "max_changes": 2}) and not m.matches(k2, {"via": "Frankfurt(Main)Hbf", "max_changes": 2})
# max_duration_h: duration_min wins; without it, arr - dep; 0/None disables the cap
assert all(s["max_duration_h"] == 6 for s in cfg["scenarios"])
assert not m.matches(dict(k2, duration_min=361), {"max_changes": 2, "max_duration_h": 6})
assert m.matches(dict(k2, duration_min=360), {"max_changes": 2, "max_duration_h": 6})
assert m.matches(dict(k2, duration_min=0), {"max_changes": 2, "max_duration_h": 6})                      # 900-600 = 300
assert not m.matches(dict(k2, duration_min=0, _arr_m=1000), {"max_changes": 2, "max_duration_h": 6})     # 400 min
assert m.matches(dict(k2, duration_min=500), {"max_changes": 2, "max_duration_h": 0})
assert not any(r["trip_id"].endswith("night") for r in rows)

# offer lookup blocked on its first call: all price searches still ran and every scenario is still logged;
# lookups go cheapest-first (the 9.99 late train) and the blocked lookup is not retried for later rows
calls.clear(); recon_ctx = []
def fake_http_recon_blocked(method, path, payload=None, **kw):
    if path == "/angebote/recon":
        calls.append(path); recon_ctx.append(payload["ctxRecon"])
        raise m.RateLimited("HTTP 403 OPS_BLOCKED")
    return fake_http(method, path, payload, **kw)
m.http_json = fake_http_recon_blocked
out_b = os.path.join(tmp, "blocked.csv")
sys.argv = ["x", "--out", out_b, "--sleep", "0"]
err = io.StringIO()
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    rc = m.main()
assert rc == 10, rc   # the alert still goes out
assert calls.count("/angebote/tagesbestpreis") == 7 and recon_ctx == ["ctx-o2026-10-22late"], (calls, recon_ctx)
assert "skipping remaining offer lookups" in err.getvalue()
with open(out_b) as f:
    rows_b = list(csv.DictReader(f))
assert {r["scenario"] for r in rows_b} == set(by) and len(rows_b) == len(rows), len(rows_b)
assert all(r["offer_name"] == "" for r in rows_b)
# same, but nothing below threshold -> exit 2 (rate-limited), rows still written
sys.argv = ["x", "--out", out_b, "--sleep", "0", "--threshold", "1"]
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    assert m.main() == 2
m.http_json = fake_http

# --only + --dates
calls.clear()
sys.argv = ["x", "--out", out, "--sleep", "0", "--only", "R-out-Thu", "--dates", "2026-10-22,2026-10-23", "--no-offers"]
with contextlib.redirect_stdout(io.StringIO()):
    rc = m.main()
assert calls.count("/angebote/tagesbestpreis") == 1   # only Thu matches the Thu scenario

# --write-config round trip
cfgp = os.path.join(tmp, "s.json")
sys.argv = ["x", "--write-config", cfgp]
with contextlib.redirect_stdout(io.StringIO()):
    assert m.main() == 0
c = json.load(open(cfgp)); c["scenarios"] = c["scenarios"][:1]; c["scenarios"][0]["earliest_dep"] = "17:00"
json.dump(c, open(cfgp, "w"))
calls.clear()
sys.argv = ["x", "--out", out, "--sleep", "0", "--config", cfgp, "--no-offers"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.main()
assert "17:38" in buf.getvalue() and "15:38" not in buf.getvalue(), buf.getvalue()

# --- rate limit: abort, exit 2, no retry
m2 = load()
class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body): self._body = body; self.code = code; self.msg = "x"; self.hdrs = None; self.fp = None
    def read(self, *a): return self._body
n = {"c": 0}
def fake_urlopen(req, timeout=0):
    n["c"] += 1; raise FakeHTTPError(403, b'{"status":"ERROR","code":"OPS_BLOCKED","errorRefId":"abc"}')
m2.HAVE_CFFI = False
m2.TRANSPORT = "urllib"   # auto would pick the browser when playwright is installed
m2.urllib.request.urlopen = fake_urlopen
try:
    m2.http_json("GET", "/reiseloesung/orte?suchbegriff=x"); raise AssertionError("expected RateLimited")
except m2.RateLimited as e:
    assert "--transport browser" in str(e), str(e)
assert n["c"] == 1
sys.argv = ["x", "--out", out, "--sleep", "0", "--transport", "urllib"]
with contextlib.redirect_stdout(io.StringIO()):
    assert m2.main() == 2
print("ALL TESTS PASSED")

# --- curl_cffi path: OPS_BLOCKED detection + no retry, using a fake session
m3 = load(); m3.HAVE_CFFI = True; m3.TRANSPORT = "curl_cffi"
class FakeResp:
    def __init__(self, code, text): self.status_code = code; self.text = text
class FakeSess:
    n = 0
    def request(self, method, url, json=None, headers=None, timeout=0):
        FakeSess.n += 1
        if "orte" in url: return FakeResp(403, '{"status":"ERROR","code":"OPS_BLOCKED"}')
        return FakeResp(200, '{"ok":1}')
m3._SESSION = FakeSess()
assert m3.http_json("GET", "/x") == {"ok": 1}
try:
    m3.http_json("GET", "/reiseloesung/orte?suchbegriff=x"); raise AssertionError("expected RateLimited")
except m3.RateLimited as e:
    assert "--transport browser" in str(e) and "pip install curl_cffi" not in str(e), str(e)
assert FakeSess.n == 2
print("CFFI PATH TESTS PASSED")

# --- browser (Playwright) path: fetch() runs inside the page; OPS_BLOCKED aborts, no retry, browser closed
m4 = load(); m4.HAVE_PLAYWRIGHT = True; m4.TRANSPORT = "browser"
class FakeBrowser:
    n = 0; closed = 0; calls = []; activity_s = []; block_first = 0
    def fetch(self, method, path, payload):
        FakeBrowser.n += 1; FakeBrowser.calls.append((method, path, payload))
        if "orte" in path: return 403, '{"status":"ERROR","code":"OPS_BLOCKED","errorRefId":"0.abc"}'
        if FakeBrowser.block_first > 0:
            FakeBrowser.block_first -= 1; return 403, '{"status":"ERROR","code":"OPS_BLOCKED"}'
        return 201, '{"ok":1}'
    def activity(self, seconds): FakeBrowser.activity_s.append(seconds)
    def close(self): FakeBrowser.closed += 1
m4._BROWSER = FakeBrowser(); m4.BROWSER_BLOCK_RETRIES = 0
assert m4.http_json("POST", "/angebote/tagesbestpreis", {"a": 1}) == {"ok": 1}
assert FakeBrowser.calls[-1] == ("POST", "/angebote/tagesbestpreis", {"a": 1})
try:
    m4.http_json("GET", "/reiseloesung/orte?suchbegriff=x"); raise AssertionError("expected RateLimited")
except m4.RateLimited as e:
    assert "--headed" in str(e), str(e)          # headless default -> hint to try headed
assert FakeBrowser.n == 2 and FakeBrowser.activity_s == []
# bounded activity-then-retry on OPS_BLOCKED (browser transport only): 2 blocks then success
m4.BROWSER_BLOCK_RETRIES = 4; m4.BROWSER_RETRY_ACTIVITY = 7.5; FakeBrowser.block_first = 2
with contextlib.redirect_stderr(io.StringIO()):
    assert m4.http_json("POST", "/angebote/tagesbestpreis", {"a": 2}) == {"ok": 1}
assert FakeBrowser.n == 5 and FakeBrowser.activity_s == [7.5, 7.5], (FakeBrowser.n, FakeBrowser.activity_s)
# ... and gives up after the retries are used (orte always blocked): 1 + 4 calls, 4 activity rounds
FakeBrowser.activity_s.clear(); FakeBrowser.n = 0
with contextlib.redirect_stderr(io.StringIO()):
    try:
        m4.http_json("GET", "/reiseloesung/orte?suchbegriff=x"); raise AssertionError("expected RateLimited")
    except m4.RateLimited as e:
        assert "4 activity retries" in str(e), str(e)
assert FakeBrowser.n == 5 and len(FakeBrowser.activity_s) == 4
m4.BROWSER_BLOCK_RETRIES = 0
assert m4.describe_transport(m4.resolve_transport()).startswith("browser (")
# main(): station lookup blocked -> exit 2, and the browser is closed in the finally
sys.argv = ["x", "--out", out, "--sleep", "0", "--transport", "browser", "--headed", "--browser-profile", "/tmp/x"]
with contextlib.redirect_stdout(io.StringIO()):
    assert m4.main() == 2
assert FakeBrowser.closed == 1 and m4._BROWSER is None
assert m4.BROWSER_HEADLESS is False and m4.BROWSER_PROFILE == "/tmp/x"
# explicit transport without its dependency -> clean SystemExit, not a traceback
m5 = load(); m5.HAVE_PLAYWRIGHT = False; m5.TRANSPORT = "browser"
try:
    m5.resolve_transport(); raise AssertionError("expected SystemExit")
except SystemExit as e:
    assert "playwright" in str(e)
m5.TRANSPORT = "auto"; m5.HAVE_CFFI = False
assert m5.resolve_transport() == "urllib"
m5.HAVE_PLAYWRIGHT = True
assert m5.resolve_transport() == "browser"
print("BROWSER PATH TESTS PASSED")
