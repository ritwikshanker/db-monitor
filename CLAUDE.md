# db-monitor — handover for Claude Code

Owner: Ritwik (works at FZJ Jülich, lives in Aachen; currently in India until ~14 Nov 2026, laptop
on IST). Written 16 Oct 2026 by a Claude (Cowork) session that could not reach bahn.de from its
cloud sandbox; everything below was verified through the user's own Chrome unless marked otherwise.

## Goal

From mid-Nov 2026 Ritwik travels Aachen ⇄ Göttingen weekly (sometimes the other person comes to
Aachen instead). He wants to know, before 14 Nov, whether DB's **Last-Minute Super Sparpreis**
(from €6.99, sold only Sat+Sun for travel Mon–Sun of the following week) ever appears for his
connections, at what time on Saturday it is released, at what price, and how fast it sells out —
then book manually each weekend at the right moment. Booking is intentionally NOT automated.
Price threshold for alerts: **€20** (he has BahnCard 25, 2nd class; adult).

## What exists

- `db_lastminute_monitor.py` — scenario-based price logger. Unit-tested offline (parsing,
  filtering, dedupe, CSV, schedule gate, rate-limit abort, all three transports with fakes).
  Live path: `--transport browser` (Playwright + installed Google Chrome), see below.
- `README_db_monitor.md` — scenario table, booking-slot logic, cron setup, CSV columns, caveats.
- `diag_bahn.py`, `diag2_bahn.py` — throwaway diagnostics (results recorded below; delete when done).
- Folder: `~/db-monitor`. Python: conda base `/opt/homebrew/anaconda3/bin/python3` (3.13);
  `curl_cffi` and `playwright` 1.64 (+ its chromium 1248) installed there; Google Chrome 155 is in
  /Applications. Cron must use that interpreter path, not `/usr/bin/python3`.

Scenarios (all: ≤ 2 changes anywhere, ≤ 6 h travel time, searched without via — since 17 Oct 2026; before: exactly 1 change
at Frankfurt(Main)Hbf; ICE/EC legs only, BC25, 2nd class):
R-out-Thu A→G Thu dep≥15:30 · R-out-Fri A→G Fri dep≥13:30 · R-back-Mon G→A Mon dep≤13:00 arr≤20:00 ·
R-back-Sun G→A Sun dep≥16:00 · P-out-Fri G→A Fri dep≥14:00 · P-back-Sun A→G Sun dep≥16:00 ·
P-back-Mon A→G Mon dep≤08:00. Cutoffs are assumptions; `--write-config scenarios.json` to edit.

## Verified facts about bahn.de (16 Oct 2026)

Endpoints (unofficial, the ones the website itself calls; same on www.bahn.de and int.bahn.de):
- `GET /web/api/reiseloesung/orte?suchbegriff=<q>&typ=ALL&limit=10` → `[{id, name, extId, type:"ST", ...}]`.
  The UI sends ids re-shaped as `A=1@O=<name>@X=..@Y=..@U=..@L=<eva>@B=1@p=<ts>@` (drop the `i=` part).
  Aachen Hbf L=8000001, Göttingen L=8000128, Frankfurt(Main)Hbf L=8000105.
- `POST /web/api/angebote/tagesbestpreis` (whole day, best price per interval) and
  `POST /web/api/angebote/fahrplan` (one time window, paging via `verbindungReference.later`).
  Body: `{abfahrtsHalt, anfrageZeitpunkt:"YYYY-MM-DDTHH:MM:SS", ankunftsHalt, ankunftSuche:"ABFAHRT",
  klasse:"KLASSE_2", produktgattungen:[ICE,EC_IC,IR,REGIONAL,SBAHN,BUS,SCHIFF,UBAHN,TRAM,ANRUFPFLICHTIG],
  reisende:[{typ:"ERWACHSENER", ermaessigungen:[{art:"BAHNCARD25", klasse:"KLASSE_2"}], alter:[], anzahl:1}],
  schnelleVerbindungen:true, zwischenhalte:[{id:<Frankfurt id>}]  ← via works ONLY in this shape (no aufenthaltsdauer key),
  autonomeReservierungOnly:false, bikeCarriage:false, reservierungsKontingenteVorhanden:false,
  nurFahrbareVerbindungen:false, nurDeutschlandTicketVerbindungen:false, deutschlandTicketVorhanden:false}`
  HTTP 201 on success. tagesbestpreis → `{intervalle:[{ab,bis,preis,verbindungen:[{abPreis:{betrag},
  klasse, teilpreis, verbindung:{tripId, ctxRecon, umstiegsAnzahl, verbindungsDauerInSeconds,
  verbindungsAbschnitte:[{verkehrsmittel:{produktGattung,name,...}, abfahrtsOrt, ankunftsOrt,
  abfahrt:{sollzeit}, ankunft:{sollzeit}, ...}]}}]}]}`. Walking legs (Frankfurt Hbf → Hbf (tief))
  have no verkehrsmittel.name / produktGattung "WALK" — filter them out.
- `POST /web/api/angebote/recon` with `{autonomeReservierung:false, einstiegsTypList:["STANDARD"],
  klasse, reisende, reservierungsKontingenteVorhanden:false, ctxRecon, deutschlandTicketVorhanden:false,
  nurDeutschlandTicketVerbindungen:false}` → `verbindungen[0].reiseAngebote[]` with `name`
  ("Super Sparpreis" / "Sparpreis" / "Flexpreis"), `klasse`, `preis.betrag`, `preisInfo.streichpreis`
  (non-BahnCard price), `konditionsAnzeigen[].kategorie|anzeigeUeberschrift`. The Last-Minute fare is
  expected to appear here by name or as an extra "Aktion"-type label (not yet observed — none on sale today).
- The site also sends header `x-correlation-id: <uuid>_<uuid>` on API calls (not required for the GET).

Route facts: Aachen→Göttingen via Frankfurt, 1 change, every 2 h: dep 07:38/09:38/11:38/13:38/15:38/17:38
(ICE 11/13/15/315/17/317 → Frankfurt Hbf, 12–14 min transfer, ICE/ECE → Göttingen), arr xx:31, 3h53.
Baseline 16 Oct with BC25: Mon 19 Oct €86.99–98.99, Tue €74.99–86.99, Wed €64.49–89.99 (Super Sparpreis).
Sun 25 Oct 2026 (checked 17 Oct, search without via): NO 1-change Frankfurt connection in either direction —
A→G needs 2 changes via Köln (e.g. 15:38 ICE 17 + ICE 653 + ICE 77, arr 20:25), G→A goes via Hannover or
Frankfurt Süd + Bus X61. Cause not stated in the API's notices. That is why scenarios now allow ≤ 2 changes.
Release time of the Last-Minute fare: DB page says only Sat+Sun; mydealz FAQ says "ab 0:01 Uhr" Saturday.

## Bot protection (Akamai Bot Manager) — what was established, 16 Oct 2026

Same public IP, minutes apart (the Mac, from India):
- Plain `urllib` / `curl` → `403 {"status":"ERROR","code":"OPS_BLOCKED"}` on everything.
- `curl_cffi` (Chrome TLS impersonation, homepage warm-up, cookies `_abck, ak_bmsc, bm_sz`) → GET `orte` ✅,
  **every POST 403** ❌.
- Playwright + **Playwright's Chromium build** (1248 / Chromium 156), headless *and* headed, `_abck` cookie
  already `~0~` (sensor accepted), `navigator.webdriver` false → GET ✅, every POST 403 ❌, including the
  SPA's *own* search POST after a deep-link search (site then shows `/kontakt/access-error?errorCode=751`).
  Only visible difference to Chrome: client-hint brands `Chromium` only. Not proven to be the cause — see
  the timing effect below; Chromium was never kept alive long enough with activity.
- Playwright + **installed Google Chrome** (`channel="chrome"`, 155): POSTs 403 for the first ~45 s of
  page life (page's own search at 6 s, our fetches at 16/27/34/41 s), then **201** at ~56 s after more
  pointer activity. With a 20 s warm-up of pointer activity the real script passed first time, headed
  **and headless**, incl. the `recon` offer lookup. 403s carry `server: AkamaiGHost` + `akamai-grn`;
  the `x-correlation-id` header is irrelevant (left out).
- **Penalty box**: ~5–10 refused POSTs within a few minutes → every POST 403 for ~10 min, whatever
  the client (hit it twice during testing; one hidden cause was a test that launched a real browser).
  Hence: bounded retries (4 × 10 s activity) and abort, never hammer. Separate burst limiter: ~15 calls
  in ~10 s → OPS_BLOCKED for a while; keep ≥3–4 s between calls.

## Browser transport (implemented) — `--transport browser`, default when Playwright is installed

`Browser` class in `db_lastminute_monitor.py`: Playwright sync API, `channel` chrome→chromium fallback
(`--browser-channel`), `--disable-blink-features=AutomationControlled`, locale de-DE, tz Europe/Berlin,
headless UA rewritten `HeadlessChrome`→`Chrome` (context `user_agent`; CDP override for
`--browser-profile` persistent contexts). Loads `/buchung/fahrplan/suche` (redirects to the homepage,
harmless), `activity()` = irregular pointer paths + occasional wheel, no clicks, for `BROWSER_SETTLE`
(20 s), then every API call = same-origin `fetch()` via `page.evaluate` with 1.5 s activity before each.
`http_json()` is still the single choke point: on `OPS_BLOCKED` it does `BROWSER_RETRY_ACTIVITY` (10 s)
+ retry up to `BROWSER_BLOCK_RETRIES` (4) times, then raises `RateLimited` → run aborts with exit 2.
`close_browser()` runs in `main()`'s `finally`. A one-date run takes ~50 s; a full weekend-slot run
(~17 calls) ~1.5–2 min. Flags: `--headed`, `--browser-channel`, `--browser-executable`, `--browser-profile`.

Open points:
- **Pi (mid-Nov)**: no Google Chrome for arm64 Linux. Untested whether Playwright's Chromium / Debian's
  `chromium` pass once given the 20 s warm-up + retries (on the Mac the Chromium build was only tried
  for ≤40 s of page life). Try `--browser-channel chromium` first, then `--browser-executable
  /usr/bin/chromium`; if both stay blocked, brand spoofing via CDP `Emulation.setUserAgentOverride`
  (`userAgentMetadata.brands` = Google Chrome) is the next cheap experiment, else the DB Navigator app
  API (db-vendo-client "dbnav" profile, `app.vendo.noncd.db.de`) — unverified.
- Akamai's scoring is statistical: expect the occasional run that needs 1–3 retries. If runs start
  failing consistently after the retries: wait 10 min, try `--headed`; `--browser-profile DIR` untested.
- The headless UA rewrite does not touch client-hint brands (`navigator.userAgentData` was not
  checked in headless; the headless run passed anyway).

## Operational notes

- Run slots are hard-coded in Berlin time (`RUN_SLOTS`, `--when-scheduled`); cron line is
  `*/15 * * * * cd ~/db-monitor && <conda python3> db_lastminute_monitor.py --when-scheduled --quiet --ntfy TOPIC >> monitor.log 2>&1`
  — works unchanged on IST laptop and on the Pi; `--show-schedule` prints slots in machine time.
  (macOS cron and Debian cron ignore `CRON_TZ`; that's why the gate is in the script.)
- macOS cron needs Full Disk Access for folders under Documents/Desktop/Downloads; `~/db-monitor` avoids it.
  Mac must be awake at slot times (`caffeinate -s`, lid open, on power).
- curl_cffi on conda-macOS: import `urllib.request` / preload CoreFoundation first, else
  `symbol not found in flat namespace '_CFArrayAppendValue'` (already handled in both scripts).
- Alerts: exit code 10 + optional `--ntfy TOPIC` (ntfy.sh, phone) + `--macos-notify`.
- Booking slot logic: Sat *S* sells Mon *S+2*…Sun *S+8*; a Monday return is booked the following
  weekend. Regular Super Sparpreis bought weeks ahead is often < €20 with BC25 → `--weeks 1,2,3,4`.

## Tests

Offline tests: `python3 tests/test_monitor2.py && python3 tests/test_sched.py` (no network; they mock `http_json`
and `datetime.now`; `load()` forces `HAVE_PLAYWRIGHT = HAVE_CFFI = False` so no test can launch a real browser
— one did, once, and triggered the penalty box). They cover parsing, scenario windows incl. cross-midnight
arrivals, request dedupe, CSV append, --only/--dates/--config, the OPS_BLOCKED abort for all three transports
(fakes), the browser transport's bounded activity-retry, flag plumbing + browser close in `finally`, and the
Berlin-time schedule gate. Live check: `python3 db_lastminute_monitor.py --dates <next Thu> --no-offers
--out /tmp/x.csv` (≈5 calls, 50 s); wait 10 min between live attempts that ended in OPS_BLOCKED.
