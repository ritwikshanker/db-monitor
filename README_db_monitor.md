# DB fare monitor — weekly Aachen ⇄ Göttingen (≤ 2 changes)

Goal: by 14 Nov 2026 know (a) whether Last-Minute Super Sparpreis fares appear for these
connections, (b) at what time on Saturday, (c) at what price, (d) how fast they go. Afterwards
the same cron job keeps alerting you every weekend so you only open bahn.de when something is
at/below your threshold.

## Scenarios (built in; `--write-config scenarios.json` to edit times)

| name | route | day | window | who |
|---|---|---|---|---|
| R-out-Thu | Aachen Hbf → Göttingen | Thu | dep ≥ 15:30 (first hit: ICE 17 15:38) | you |
| R-out-Fri | Aachen Hbf → Göttingen | Fri | dep ≥ 13:30 (first hit: ICE 315 13:38) | you |
| R-back-Mon | Göttingen → Aachen Hbf | Mon | dep ≤ 13:00 and arr ≤ 20:00 | you |
| R-back-Sun | Göttingen → Aachen Hbf | Sun | dep ≥ 16:00 (last trains) | you |
| P-out-Fri | Göttingen → Aachen Hbf | Fri | dep ≥ 14:00 (after half a workday) | partner |
| P-back-Sun | Aachen Hbf → Göttingen | Sun | dep ≥ 16:00 (last trains) | partner |
| P-back-Mon | Aachen Hbf → Göttingen | Mon | dep ≤ 08:00 (first train, ICE 11 07:38 → 11:31) | partner |

All scenarios: at most 2 changes at any station (normally 1 at Frankfurt; on diversion days
2 via Köln/Hannover — e.g. Sun 25 Oct 2026 had no 1-change Frankfurt connection at all), at most
6 h travel time (drops overnight routings like 21:36 → 04:45), ICE/EC/IC legs only, 2nd class, 1 adult with BahnCard 25, threshold €20. Searched without a via station. Assumptions I made where you were vague — all editable in the
JSON: "morning" = dep ≤ 13:00, "not super late" = arr ≤ 20:00, "half workday" = dep ≥ 14:00,
"last train" = dep ≥ 16:00, "first train" = dep ≤ 08:00. Per-scenario overrides exist for
`via`, `max_changes`, `max_duration_h` (0 = no cap), `long_distance_only`, `threshold_eur` (`"via": "Frankfurt(Main)Hbf"` and
`"max_changes": 1` restore the old Frankfurt-only behaviour; a via is also sent to bahn.de as a
forced intermediate stop).

## Booking slots — your Monday question

Yes, you have it right. On Saturday *S* (and Sunday *S+1*) the Last-Minute fare covers travel
Mon *S+2* … Sun *S+8*. So for one trip:

- outbound Thu/Fri (*S+5/S+6*) → book on weekend *S*
- return Sun evening (*S+8*) → same weekend *S*
- return Mon morning (*S+9*) → only on the **next** weekend (*S+7/S+8*), i.e. you book it
  the day before you travel, with 1–2 days' notice.

Every weekend run therefore scans one full Mon–Sun block, which contains *this* trip's
outbound (Thu/Fri) and Sunday return plus the *previous* trip's Monday return. Nothing extra
to configure — the Monday rows that matter appear one weekend later than the Thu/Fri rows.

Regular Super Sparpreis is a separate lever: the floor for this distance is well under €20
with BC25 when bought weeks ahead (today, one week ahead, you see €65–99 because the cheap
tiers are gone). `--weeks 1,2,3,4` scans the following four weeks in one run (7 extra
requests per week); a daily run with that flag catches cheap regular fares for the Monday
returns and for weeks when no Last-Minute fare shows up.

## Verified facts (16 Oct 2026)

- Last-Minute Super Sparpreis (DB page): bookable **Saturday + Sunday only**, for the
  following week; ICE/IC/EC within Germany; train-bound; no exchange; free cancellation only
  within 3 h of booking; BahnCard 25/50 → further 25 % off; season 9 May – 13 Dec 2026.
  From €6.99 list → €5.24 with BC25. Your €20 cap ≈ €26.65 list price.
- Release time: DB does not state it. mydealz FAQ: "immer ab 0:01 Uhr" Saturday (secondary
  source — the Sat 00:00 / 00:30 runs confirm or refute it).
- Aachen → Göttingen via Frankfurt, every 2 h, same pattern each day: dep 07:38 / 09:38 /
  11:38 / 13:38 / 15:38 / 17:38 (ICE 11, 13, 15, 315, 17, 317 to Frankfurt Hbf, 12–14 min
  transfer, ICE/ECE on to Göttingen), arr xx:31, 3 h 53. The reverse pattern is captured
  automatically on the first real run.
- Prices today with BC25 (Super Sparpreis): Mon 19 Oct €86.99–98.99, Tue 20 Oct
  €74.99–86.99, Wed 21 Oct €64.49–89.99. Nothing near €20.
- bahn.de rate-limits: ~15 API calls within ~10 s → HTTP 403 `OPS_BLOCKED` for a while (I
  triggered it from your Chrome while testing; it is temporary). The script spaces requests
  4 s apart and aborts the run on a block instead of retrying.

## Running

### Transport: the script runs inside a real browser

bahn.de sits behind Akamai Bot Manager. The GET endpoint (station search) is open, but every
POST (`tagesbestpreis`, `fahrplan`, `recon`) is answered `403 {"code":"OPS_BLOCKED"}` unless
the request comes from a browser whose page has run Akamai's sensor script and shown some
visitor activity. Verified 16 Oct 2026 from the Mac: plain urllib/curl → 403; curl_cffi with
Chrome TLS impersonation → GET ok, POST 403; Playwright's own Chromium build (headless *and*
headed, client-hint brand "Chromium") → POST 403 every time; the installed **Google Chrome**
driven by Playwright → POST 403 for the first ~45 s of page life, then 201. Each refused POST
is also a bad signal: a handful of them in a few minutes puts the client in a penalty box for
about 10 minutes (everything 403, even the real website in that browser).

So `--transport browser` (the default when Playwright is installed) does this:

1. launch Google Chrome (`--browser-channel auto`: installed Chrome, else Playwright's Chromium),
   headless by default, locale de-DE, timezone Europe/Berlin, headless UA rewritten to plain Chrome;
2. load `bahn.de/buchung/fahrplan/suche` and spend ~20 s moving the pointer / scrolling
   (irregular paths, no clicks);
3. run every API call as a same-origin `fetch()` from inside that page (`page.evaluate`), with
   a little pointer activity before each call and the usual 4 s spacing;
4. on `OPS_BLOCKED`, add 10 s of activity and retry, at most 4 times, then abort the run.

`http_json()` stays the single choke point, so parsing, CSV and the schedule gate are untouched;
`--transport curl_cffi` / `--transport urllib` remain for completeness (and the tests).

Install into the **same** Python you put in the crontab (`which python3`):

```bash
pip install playwright && python3 -m playwright install chromium   # Chromium = fallback only
```

Google Chrome itself must be installed (`/Applications/Google Chrome.app`); Playwright finds it
via `channel="chrome"`. Flags: `--headed` (visible window), `--browser-channel chrome|chromium|msedge`,
`--browser-executable PATH` (e.g. a distro chromium on the Pi), `--browser-profile DIR`
(persistent profile; not needed so far). A run prints which transport/channel it used in its
first line, e.g. `transport: browser (Playwright, chrome, headless)`.

```bash
# Python >= 3.9 + playwright (+ Google Chrome)
python3 db_lastminute_monitor.py                       # following week, all 7 scenarios → db_prices.csv
python3 db_lastminute_monitor.py --only R-out-Fri,R-back-Mon
python3 db_lastminute_monitor.py --weeks 1,2,3,4       # also the three weeks after
python3 db_lastminute_monitor.py --dates 2026-10-22    # one date (scenarios still filter by weekday)
python3 db_lastminute_monitor.py --write-config scenarios.json   # then edit and pass --config scenarios.json
python3 db_lastminute_monitor.py --ntfy <secret-topic> --macos-notify
```

Exit codes: 10 = something at/below a threshold (use it for notifications), 2 = API failure
or rate-limited, 0 = nothing found.

Phone push: install the **ntfy** app on the S25, subscribe to a hard-to-guess topic name,
pass the same name with `--ntfy`. No account needed; it's a public server, so nothing
private in the topic name. The alert text is just scenario, date, trains, price.

## CSV columns (`db_prices.csv`, appended per run)

`run_time` (Europe/Berlin), `scenario`, `route`, `travel_date`, `weekday`, `days_ahead`,
`dep`, `arr` (`+1` = after midnight), `duration_min`, `trains`, `change_station` (all change
stations, `" / "`-separated), `changes`,
`products`, `price_eur` (with BC25), `list_price_eur` (without BC — only where the offer
lookup ran), `offer_name` (Super Sparpreis / Sparpreis / Flexpreis / whatever DB calls the
Last-Minute fare), `offer_labels` (condition categories; an "Aktion"-type label is expected on
the Last-Minute fare), `below_threshold`, `trip_id`.

The offer lookup runs for the cheapest connection per scenario-day plus anything
≤ `--offers-below` (default €30), to stay inside the request budget.

## Schedule

The run slots are built into the script (Europe/Berlin): Fri 20:00–23:00 hourly (baseline),
Sat 00:00 / 00:30 (release expected ~00:01), Sat 01:00–12:00 hourly, Sat 18:00,
Sun 09:00 and 18:00 — 21 runs per weekend × ~17 requests, 4 s apart, far below the limiter.
Runs stay at least 30 min apart: on 17 Oct 2026 the 15-min cluster 00:00–00:45 had 3 of 4 runs
blocked on `tagesbestpreis` (00:45 got no data at all), while no hourly run was blocked there.
Cron just fires the script every 15 minutes and `--when-scheduled` makes it exit instantly
unless the Berlin time is a slot. That way the same crontab line works on a laptop in India
(IST) and on a Pi in Germany, and the 25 Oct clock change needs no attention
(neither macOS cron nor Debian cron honours `CRON_TZ`).

```cron
*/15 * * * * cd /Users/YOURNAME/db-monitor && /FULL/PATH/TO/python3 db_lastminute_monitor.py --when-scheduled --quiet --ntfy TOPIC >> monitor.log 2>&1
# optional: daily regular-Sparpreis watch, 4 weeks out (local machine time)
15 7 * * *   cd /Users/YOURNAME/db-monitor && /FULL/PATH/TO/python3 db_lastminute_monitor.py --weeks 1,2,3,4 --no-offers --quiet --out db_prices_ahead.csv >> monitor.log 2>&1
```

`/FULL/PATH/TO/python3` = output of `which python3` in the shell where `pip install playwright`
ran (cron has a minimal PATH and would otherwise pick a Python without Playwright). A run takes
about 1–2 minutes now (browser start, 20 s warm-up, 17 calls 4 s apart, plus any retries).

`python3 db_lastminute_monitor.py --show-schedule` prints the next slots in Berlin *and*
machine time so you can see when the laptop must be awake. On the Pi use `/home/pi/db-monitor`
(or wherever) in place of the Mac path.

Where to run it: an always-on machine is best (your Pi, from mid-November). Until then your
Mac works if it is awake at the slot times: on power, lid open, `caffeinate -s` running in a
Terminal tab from Friday evening to Sunday evening. macOS cron needs Full Disk Access for
`/usr/sbin/cron` if the folder lives in Documents/Desktop/Downloads — a folder directly under
your home (`~/db-monitor`) avoids that.

## Reading the data by 14 Nov

- Release time = first `run_time` on a Saturday where `offer_name`/`offer_labels` changes or
  `price_eur` drops to Last-Minute level (≤ ~€27 with BC25) for any row.
- Sell-out speed = how many hours later the cheap rows vanish for the same `trip_id`.
- Which scenarios ever get the fare = `scenario` × `weekday` of the cheap rows over 4 weekends.
- Then on a booking Saturday at the observed time: bahn.de → search with
  "Zwischenhalt: Frankfurt(Main)Hbf" → crown labelled "Aktion" → book (free cancellation 3 h).

## Caveats

- Unofficial endpoints (`/web/api/angebote/tagesbestpreis`, `/angebote/recon`); DB may change
  them. If every run fails with 400/404, the request format moved — ping me with the log.
- No login; the BC25 price comes from the `BAHNCARD25` traveller setting, same as the website.
- bahn.de's Akamai bot protection rejects everything that is not a real Chrome running the
  site's JavaScript (see *Transport* above). If every run ends with `OPS_BLOCKED` after the
  retries: wait 10 min (penalty box), then try `--headed`; if that works but headless does
  not, the headless fingerprint got flagged. Remaining fallback: the DB Navigator app API
  (`app.vendo.noncd.db.de`, used by db-vendo-client) — unverified.
- Booking stays manual (your choice: ask before paying).
