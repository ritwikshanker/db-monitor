# db-monitor

Logs bahn.de prices for a fixed set of weekly train trips, here Aachen Hbf ⇄ Göttingen with one
change at Frankfurt(Main)Hbf, to find out whether DB's **Last-Minute Super Sparpreis** shows up
for them, when it is released on Saturday, at what price, and how quickly it sells out. When a
connection is at or below a price threshold it sends an alert. You still book by hand; the
script never books.

> Not affiliated with Deutsche Bahn. It uses the same unofficial JSON endpoints the bahn.de
> website calls, which can change without notice. Keep the request rate low (see *Rate limits*).

## How it works

- A **scenario** is a route (origin, destination, optional via station, exact number of
  changes), a weekday and a departure/arrival time window. Seven are built in (outbound
  Thu/Fri, returns Sun/Mon, both directions); `--write-config scenarios.json` dumps them for
  editing.
- Each run maps the scenarios onto the dates of the following week (the week Last-Minute fares
  are sold for on Sat/Sun). It queries each (route, date) once, filters by every scenario's
  window, looks up the fare name for the cheapest hits, and appends one row per connection to
  `db_prices.csv`.
- bahn.de sits behind Akamai Bot Manager. Only a real Google Chrome running the site's own
  JavaScript gets its POST requests through, so the default `browser` transport drives Chrome
  through Playwright and sends every API call as a same-origin `fetch()` from inside the page.
- A Berlin-time schedule gate (`--when-scheduled`) lets one `*/15` cron line run on any
  machine timezone. The slots are Fri evening, Sat around midnight and hourly, and Sun.

## Requirements

- Python ≥ 3.9 (developed with 3.13)
- [Playwright](https://playwright.dev/python/) and an installed **Google Chrome**. Playwright's
  own Chromium is the fallback, but it was blocked in testing.
- Optional: `curl_cffi`, for the alternative transport. Its GETs pass, its POSTs are blocked.

```bash
pip install playwright
python3 -m playwright install chromium
```

## Usage

```bash
python3 db_lastminute_monitor.py                      # following week, all scenarios → db_prices.csv
python3 db_lastminute_monitor.py --only R-out-Fri,R-back-Mon
python3 db_lastminute_monitor.py --weeks 1,2,3,4      # also regular Sparpreis 2–4 weeks ahead
python3 db_lastminute_monitor.py --dates 2026-10-22 --no-offers --out /tmp/x.csv   # quick live check
python3 db_lastminute_monitor.py --show-schedule      # next run slots in Berlin and local time
python3 db_lastminute_monitor.py --ntfy <topic> --macos-notify
```

Cron (use the full path of the Python that has Playwright installed):

```cron
*/15 * * * * cd ~/db-monitor && /path/to/python3 db_lastminute_monitor.py --when-scheduled --quiet --ntfy TOPIC >> monitor.log 2>&1
```

Exit codes: `0` nothing found, `10` at least one connection at or below the threshold
(default €20 with BahnCard 25), `2` API failure or rate-limited (the run aborts).

## Rate limits

- About 15 calls in about 10 s triggers `403 OPS_BLOCKED` for a while. The script keeps 3–4 s
  between calls.
- About 5–10 refused POSTs within a few minutes block every POST for about 10 minutes. The
  browser transport retries a bounded number of times and then aborts instead of hammering.
- A full run takes about 1.5–2 min.

## Tests

The tests run offline and never touch the network or launch a browser:

```bash
python3 tests/test_monitor2.py && python3 tests/test_sched.py
```

## Files

| file | what |
|---|---|
| `db_lastminute_monitor.py` | the monitor (single file) |
| [`README_db_monitor.md`](README_db_monitor.md) | detailed notes: scenario table, booking-slot logic, CSV columns, schedule, caveats |
| [`CLAUDE.md`](CLAUDE.md) | handover notes: verified API shapes and bot-protection findings |
| `diag_bahn.py`, `diag2_bahn.py` | one-off diagnostics from the bot-protection investigation |
| `weekend_report_*.md` | per-weekend findings |
| `tests/` | offline unit tests |
