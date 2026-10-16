# Weekend report — Sat 17 Oct 2026: PENDING, no data yet

Status at Fri 16 Oct 2026, 17:41 Berlin (21:11 IST): the first scheduled slot is **Fri 20:00 Berlin
(23:30 IST)**, and the last is **Sun 18 Oct 18:00 Berlin (21:30 IST)**. Neither `monitor.log` nor
`db_prices.csv` exists yet (the 16 Oct live check did not write to `db_prices.csv`). So the tables and
the conclusion can't be written yet. This file will be replaced by the generated report after
Sunday 18:00 Berlin.

## How to produce the real report (offline, no bahn.de traffic)

A stdlib-only script was written and tested on synthetic data (missed slots, a partial run, a fake
"Aktion" fare appearing and disappearing). It is in the session notes folder:

```bash
python3 -I "$HOME/Library/Application Support/Claude/side-session-notes/local_6ac31812-cba9-45b8-821f-5b1d47890100/weekend_report.py" --dir ~/db-monitor --saturday 2026-10-17 --out ~/db-monitor/weekend_report_2026-10-17.md
```

It writes:
1. **Coverage per slot:** CSV rows present, complete, partial or none.
2. **Lowest price per scenario and travel date per run:** a pivot table with `*` for Last-Minute or
   Aktion in `offer_name`/`offer_labels`, and `!` for prices at or below €20.
3. **Flagged fares:** first-seen and first-gone run for each one. A run that skipped the
   scenario-day does not count as "gone".
4. **A draft conclusion.**

To check whether the Mac slept through a slot, run `pmset -g log | grep -E "Sleep|Wake"` for
Fri 20:00–Sun 18:00 Berlin (= Fri 23:30–Sun 21:30 IST).

## Problems to fix, ideally before 20:00 Berlin tonight

1. **`monitor.log` cannot answer "which runs happened or aborted".** With `--quiet`, a successful run
   prints nothing at all (`log` is a no-op; `db_lastminute_monitor.py:813`). The `[error]`/`[warn]`
   lines that do reach the log have no timestamp, and the exit code (0/2/10) is never logged.
   - What this means: runs can be counted only from distinct `run_time` values in the CSV.
   - Not distinguishable: a slot with no rows could mean the Mac was asleep, cron did not fire, the
     run aborted before the first search, or no connection matched.
   - Fix: in `--when-scheduled` mode, always print one closing line, even with `--quiet`, e.g.
     `<run_time> slot-run rows=<n> route-days=<ok>/<total> exit=<code>`. Also prefix the `[error]` and
     `[warn]` lines with `run_time`.
2. **23 slots, not 24.** `RUN_SLOTS` (`db_lastminute_monitor.py:477`) has 4 (Fri 20–23) + 4 (Sat
   00:00–00:45) + 12 (Sat 01–12) + 1 (Sat 18) + 2 (Sun 09, 18) = **23**. The README and the review
   brief both say 24. Either add the missing slot or correct the README. The README also says "the
   Sat 00:05 run" in one place, but the slot is 00:00.
3. **An OPS_BLOCKED during an offer lookup ends the whole run, but the message says otherwise.** The
   message says "skipping remaining offer lookups this run". In fact `blocked = True` also stops all
   remaining route-days (`if blocked: break`, `db_lastminute_monitor.py:888`). The rows collected so
   far are still written, so this shows up as a *partial* run. Expect partial runs, and either change
   the message or skip only the offers.
4. **The ntfy topic `db_alert` is public and guessable** (the cron-setup note mentions this too).
   Anyone can read or spam it. Delivery to the phone has not been tested.
5. **The Sat 00:00 run starts before the expected release.** It starts ~00:00:0x plus ~50 s of
   warm-up, which straddles the "ab 0:01" release mentioned in the mydealz FAQ. A fare first seen at
   00:15 means release between ~00:01 and 00:15, not "late".

## Conclusion

Not yet possible. The Last-Minute fare cannot have been observed: the first collection slot (Fri
20:00 Berlin) is still ahead, and the Saturday release slots (00:00–00:45 Berlin) come after that.
Run the command above after Sun 18 Oct 18:00 Berlin. Fixing point 1 before tonight is what decides
whether this weekend's aborted runs can be told apart from missed ones.
