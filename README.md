# Argora

Free Google Maps lead-gen for **no-website prospecting** — find local businesses
that have no website while their same-category competitors do, then pitch them one.

No Apify credits, no Google Places API. A maintained-style Playwright scraper does
the extraction; the value lives in the **lead-intelligence layer** on top (sector
presets, website split, dedupe, ranking, competitor lists).

## Architecture — why it's split this way

```
app.py           ← THE one command — launches the web app + opens your browser
webapp/
  server.py      ← FastAPI backend: runs jobs, streams live logs (SSE), SQL API
  static/
    index.html   ← the minimalist UI (one file, no build step)
leadfinder/
  scraper.py     ← FRAGILE. Drives Google Maps, pulls raw places. The SEL dict is
                   the ONE place to fix when Google changes its DOM.
  analyze.py     ← STABLE + TESTED. Raw JSON → LEADS / COMPETITORS / ALL csvs.
                   Auto-normalizes columns, so it ingests output from ANY engine
                   (this scraper, omkarcloud, gosom, Apify) unchanged.
  sql_gen.py     ← STABLE + TESTED. csv → Supabase `leads` INSERT sql (idempotent).
  sectors.py     ← Your presets (driving-school, real-estate, gym, dentist…). A dict.
run.py           ← CLI alternative: scrape a sector in an area, then build the csvs.
data/raw/        ← raw JSON pulls (git-ignored)
data/leads/      ← the deliverables — csv (git-ignored)
data/sql/        ← generated Supabase sql (git-ignored)
```

The split is deliberate: the scraper is a maintenance treadmill (selectors rot,
IPs throttle). The analysis layer — your real moat — never breaks when Google
changes its markup.

## Setup (one time)

```bash
cd ~/Documents/Beta/maps-lead-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run it — the web app (recommended)

```bash
python app.py
```

Your browser opens at `http://127.0.0.1:8000`. From there you:

1. **Pick categories** (chips) + a **location**, set max/search, hit **▶ Go**.
2. Watch the scrape **live** — every place streams into the console as it's
   visited, with running lead/competitor/scraped counts.
3. Browse the **extracted files** (preview + download) — all written to
   `data/leads/` (git-ignored).
4. **Generate Supabase SQL** from any LEADS file — pick it, set the dataset
   name, click *Generate SQL*, copy or download. (See "Pushing to Supabase".)

One job runs at a time (a scrape opens a real browser). `Ctrl-C` stops the server.

## Run it — the CLI (scriptable)

```bash
# one sector, one area — scrape + analyze in one go
python run.py driving-school "Jayanagar, Bengaluru"

# bigger area, custom cap
python run.py real-estate "Whitefield, Bengaluru" --max 120
```

Output: `data/leads/driving-school-jayanagar-bengaluru-LEADS.csv` (+ `-COMPETITORS`, `-ALL`).

## Pushing to Supabase (KunthiveOS)

The SQL generator (`leadfinder/sql_gen.py`, or the web app's section 4) turns a
LEADS csv into an `INSERT INTO leads (...)` that matches the **real KunthiveOS
schema** (`KunthiveOS/supabase/migrations/0001_init.sql`) — enum-correct
`website_status`/`source`, a separate `website_url`, and a 0–100 `score`.

```bash
python -m leadfinder.sql_gen data/leads/gym-yelahanka-LEADS.csv \
    --dataset argora/gym-yelahanka --out data/sql/gym-yelahanka.sql
```

Then open Supabase → **SQL editor** and paste it. **It only ever INSERTs, and it
can never add anything twice** — guarded four ways (intra-batch, `WHERE NOT
EXISTS` on place-id *or* phone, `ON CONFLICT` on the unique constraint,
INSERT-only). Verified end-to-end against Postgres 16 with this schema.

Full column mapping and the duplicate guarantees: see **SCHEMA.md**.

### Merge several areas into one deduped list

Scrape each area, then run the analyzer over all the raw files at once:

```bash
python -m leadfinder.scraper "real estate agent" "Whitefield, Bengaluru"  --out data/raw/re-whitefield.json
python -m leadfinder.scraper "real estate agent" "HSR Layout, Bengaluru"   --out data/raw/re-hsr.json
python -m leadfinder.analyze "data/raw/re-*.json" --out data/leads/real-estate --sector real-estate
```

## The three output files

| File | Rows | Use |
|---|---|---|
| `…-LEADS.csv` | no website + has phone, ranked | **who you call** |
| `…-COMPETITORS.csv` | same category, HAS a website, ranked | pitch ammo: "your neighbour X has a site + N reviews" |
| `…-ALL.csv` | everything, `website_status` column | the full picture / QA |

Ranking = `reviews × rating` — established businesses that simply never went
online surface first.

## Before you scale — read BARRIERS.md

`BARRIERS.md` is the must-read: what gets you throttled/banned and what to never
build. The short version:

- **Google caps ~120 results per search.** Scrape area-by-area, not one big query.
- **Headed + gentle + spaced-out.** Headless at volume is the easiest bot tell.
- **No evasion tooling** (CAPTCHA solvers, proxy rotation, login automation) —
  that's the line between grey-area prospecting and ToS abuse.
- **Logged out**, always. Never drive Maps signed into your Google account.
- **Data stays local** — `data/` is git-ignored; it's business PII.
- **Selectors rot** — when extraction goes blank, fix the `SEL` dict in `scraper.py`.

## Adding a sector

Edit `leadfinder/sectors.py` — copy a block, change `query`, `exclude`
(keywords that mean "wrong business"), and `min_reviews`. That's it.

## Note

Public business data, modest volume, for your own outreach (Kunthive). This sits
in Google's ToS grey zone — fine for small B2B prospecting, riskier at high
volume. Don't add CAPTCHA-evasion machinery; keep runs small and outreach legit.

## Relationship to `../maps-lead-gen`

`maps-lead-gen` is the **Apify-based** version (pay-per-event, `compass/crawler-google-places`).
This repo is the **free Playwright** version. `analyze.py` here is column-compatible
with that repo's Apify JSON, so you can mix sources.
