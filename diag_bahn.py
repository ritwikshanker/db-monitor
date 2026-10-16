#!/usr/bin/env python3
"""Which client fingerprint does bahn.de accept from this machine? Tries 5 variants, 5 s apart."""
import json, shutil, subprocess, sys, time, urllib.request, urllib.error

URL = "https://www.bahn.de/web/api/reiseloesung/orte?suchbegriff=Aachen%20Hbf&typ=ALL&limit=1"
URL_INT = URL.replace("www.bahn.de", "int.bahn.de")
UA_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

def urllib_try(label, url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"{label:<34} HTTP {r.status}  ok={'Aachen Hbf' in body}")
    except urllib.error.HTTPError as e:
        body = e.read()[:120].decode("utf-8", "replace")
        print(f"{label:<34} HTTP {e.code}  {body}")
    except Exception as e:
        print(f"{label:<34} ERROR {e}")

def curl_try(label, url, extra):
    if not shutil.which("curl"):
        print(f"{label:<34} curl not installed"); return
    cmd = ["curl", "-s", "-o", "/dev/stdout", "-w", "\\nHTTP %{http_code}", "--max-time", "30", url] + extra
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    status = out.rsplit("HTTP ", 1)[-1].strip()
    print(f"{label:<34} HTTP {status}  ok={'Aachen Hbf' in out}  {'' if 'Aachen Hbf' in out else out[:100].strip()}")

print(sys.version.split()[0], "on", sys.platform)
urllib_try("urllib, script headers", URL, {"Accept": "application/json", "User-Agent": UA_CHROME,
                                           "Accept-Language": "de-DE,de;q=0.9", "Origin": "https://www.bahn.de",
                                           "Referer": "https://www.bahn.de/buchung/fahrplan/suche"})
time.sleep(5)
urllib_try("urllib, Accept only (python UA)", URL, {"Accept": "application/json"})
time.sleep(5)
urllib_try("urllib, int.bahn.de", URL_INT, {"Accept": "application/json", "User-Agent": UA_CHROME})
time.sleep(5)
curl_try("curl, default UA", URL, ["-H", "Accept: application/json"])
time.sleep(5)
curl_try("curl, Chrome UA", URL, ["-H", "Accept: application/json", "-A", UA_CHROME])
time.sleep(5)
try:
    from curl_cffi import requests as cr  # optional, only if installed
    r = cr.get(URL, impersonate="chrome", headers={"Accept": "application/json"}, timeout=30)
    print(f"{'curl_cffi impersonate=chrome':<34} HTTP {r.status_code}  ok={'Aachen Hbf' in r.text}")
except ImportError:
    print(f"{'curl_cffi':<34} not installed (pip install curl_cffi) — skip")
except Exception as e:
    print(f"{'curl_cffi':<34} ERROR {e}")
