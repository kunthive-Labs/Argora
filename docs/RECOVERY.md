# Recovery — what survives an interruption, and how to pick up the pieces

Scrapes get interrupted. The common causes, roughly in order of likelihood:

1. **Google's CAPTCHA / throttle wall** ("our systems have detected unusual
   traffic") — you've been rate-limited.
2. **You hit Halt** in the UI.
3. **A flaky page** — one place's detail page times out.
4. **The browser dies** (Playwright crash, window closed by hand).
5. **The whole process dies** — laptop sleep/battery, `kill -9`, a crash.

Argora's durability model is: **every scraped place is on disk within a moment
of being scraped, and every finished target's outcome is on disk before the
next one starts.** Nothing above costs you more than the single card that was
in flight.

---

## The three layers

### 1. Write-through raw checkpoints (survives everything, even `kill -9`)

The scraper calls a checkpoint after **every extracted place**, atomically
rewriting `data/raw/<trade>-<area>.json` (write to temp file, then rename — a
kill mid-write can never corrupt it). This is the ground truth: if the process
died with 87 places scraped, the raw JSON has 87 places (or 86, if it died
mid-write of the 87th).

### 2. Per-target isolation + `-RECOVERED` books (scrape-level failures)

When a scrape raises (CAPTCHA, timeout storm, browser death), the partial
results it carried are analyzed and written as CSVs immediately, with a
`-RECOVERED` suffix:

```
data/leads/gym-hsr-layout-RECOVERED-LEADS.csv
data/leads/gym-hsr-layout-RECOVERED-COMPETITORS.csv
data/leads/gym-hsr-layout-RECOVERED-ALL.csv
```

A `-RECOVERED` book is a normal book in every way (fol. 3 browses it, fol. 4
pushes it, fol. 6 works it) — the suffix only tells you it came from an
incomplete scrape.

**Non-fatal** failures (a flaky target) are recorded on that target's summary
row and the run **continues** to the remaining trades/areas. **Fatal** failures
stop the whole run:

- the CAPTCHA wall — continuing would mean fighting the throttle, which
  BARRIERS.md forbids; stop for the day.
- the browser disconnecting — nothing left to drive.

### 3. Job snapshots (survives a dead server)

`data/last_job.json` is rewritten (atomically) after **every finished target**,
flagged `"in_progress": true`, and finalized when the run ends. If the server
process dies mid-run, reopening the app shows the last checkpoint in fol. 2:
*"interrupted mid-run (partials saved to data/)"*.

---

## Recovering by hand

Usually there's nothing to do — the `-RECOVERED` CSVs are already built. The
one case that needs a manual step is a **hard process death** (layer 1 saved
the raw JSON, but nobody got to run the analysis). Rebuild the CSVs from the
checkpoint:

```bash
# from the raw checkpoint of the interrupted target:
python -m leadfinder.analyze data/raw/gym-hsr-layout.json \
    --out data/leads/gym-hsr-layout-RECOVERED --sector gym
```

(`--sector` applies the preset's excludes + review floor; for a custom trade,
use `--min-reviews`/`--exclude` directly or omit them.)

That's the whole recovery path: raw JSON in, ranked LEADS / COMPETITORS / ALL
books out. Re-scraping the same area later simply overwrites the stem's files
with the complete versions.

## After a CAPTCHA wall

Stop for the day. Really. The wall means Google flagged your IP's traffic —
re-running now digs the hole deeper, and per **BARRIERS.md** this project will
never include solving/evasion tooling. Your partials are already saved and
pushable; work the leads you have, scrape again tomorrow, gentler.

## What is *not* recoverable

- The single place that was mid-extraction when the process died.
- Log lines streamed to a browser tab you closed (the last 500 are in
  `data/last_job.json`, so usually nothing is truly lost).
