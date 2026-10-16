#!/usr/bin/env python3
"""Second diagnostic: which curl_cffi variant gets the POST (tagesbestpreis) through? 6 s between tries."""
import json, sys, time, uuid
import urllib.request  # macOS: loads CoreFoundation before curl_cffi (avoids '_CFArrayAppendValue' dlopen error)
if sys.platform == "darwin":
    import ctypes
    ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
from curl_cffi import requests as cr

AACHEN = "A=1@O=Aachen Hbf@X=6091495@Y=50767803@U=80@L=8000001@B=1@p=1792001962@"
GOETT = "A=1@O=Göttingen@X=9926069@Y=51536812@U=80@L=8000128@B=1@p=1792001962@"
FFM = "A=1@O=Frankfurt(Main)Hbf@X=8662833@Y=50106682@U=80@L=8000105@B=1@p=1792001962@"
BODY = {
    "abfahrtsHalt": AACHEN, "anfrageZeitpunkt": "2026-10-22T00:00:00", "ankunftsHalt": GOETT,
    "ankunftSuche": "ABFAHRT", "klasse": "KLASSE_2",
    "produktgattungen": ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"],
    "reisende": [{"typ": "ERWACHSENER", "ermaessigungen": [{"art": "BAHNCARD25", "klasse": "KLASSE_2"}], "alter": [], "anzahl": 1}],
    "schnelleVerbindungen": True, "zwischenhalte": [{"id": FFM}], "autonomeReservierungOnly": False,
    "bikeCarriage": False, "reservierungsKontingenteVorhanden": False, "nurFahrbareVerbindungen": False,
    "nurDeutschlandTicketVerbindungen": False, "deutschlandTicketVorhanden": False,
}

def corr():
    return f"{uuid.uuid4()}_{uuid.uuid4()}"

def attempt(label, host, impersonate, warm, extra_headers, path="/web/api/angebote/tagesbestpreis"):
    s = cr.Session(impersonate=impersonate)
    cookies = "-"
    try:
        if warm:
            w = s.get(f"https://{host}/", timeout=30, allow_redirects=True)
            cookies = ",".join(sorted(s.cookies.keys())) or "(none)"
            warm_info = f"warm {w.status_code} -> {w.url.split('/')[2]}"
        else:
            warm_info = "no warm-up"
        headers = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
                   "Accept-Language": "de-DE,de;q=0.9,en;q=0.8", "Origin": f"https://{host}",
                   "Referer": f"https://{host}/buchung/fahrplan/suche", **extra_headers}
        r = s.post(f"https://{host}{path}", json=BODY, headers=headers, timeout=30)
        ok = r.status_code == 200 or r.status_code == 201
        n = len(r.json().get("intervalle", [])) if ok else 0
        print(f"{label:<42} HTTP {r.status_code}  intervals={n}  [{warm_info}; cookies: {cookies}]"
              + ("" if ok else f"  {r.text[:90]}"))
        return ok
    except Exception as e:
        print(f"{label:<42} ERROR {e}")
        return False

tries = [
    ("A chrome, www, warm, no corr-id",      "www.bahn.de", "chrome", True,  {}),
    ("B chrome, www, warm, x-correlation-id", "www.bahn.de", "chrome", True,  {"x-correlation-id": corr()}),
    ("C chrome, int.bahn.de, warm, corr-id",  "int.bahn.de", "chrome", True,  {"x-correlation-id": corr()}),
    ("D chrome, www, NO warm, corr-id",       "www.bahn.de", "chrome", False, {"x-correlation-id": corr()}),
    ("E safari, www, warm, corr-id",          "www.bahn.de", "safari", True,  {"x-correlation-id": corr()}),
    ("F chrome, www, warm, corr-id, fahrplan","www.bahn.de", "chrome", True,  {"x-correlation-id": corr()}),
]
for i, t in enumerate(tries):
    label, host, imp, warm, hdr = t
    path = "/web/api/angebote/fahrplan" if label.startswith("F") else "/web/api/angebote/tagesbestpreis"
    if label.startswith("F"):
        BODY["anfrageZeitpunkt"] = "2026-10-22T13:00:00"
    ok = attempt(label, host, imp, warm, hdr, path)
    if ok:
        print("-> this variant works; stopping here.")
        break
    if i < len(tries) - 1:
        time.sleep(6)
