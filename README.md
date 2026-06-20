# Argora

Free Google Maps lead-gen for **no-website prospecting** — find local businesses
that have no website while their same-category competitors do, then pitch them one.

No Apify credits, no Google Places API. A maintained-style Playwright scraper does
the extraction; the value lives in the **lead-intelligence layer** on top (sector
presets, website split, dedupe, ranking, competitor lists).

## Architecture — why it's split this way

```
leadfinder/
  scraper.py   ← FRAGILE. Drives Google Maps, pulls raw places. The SEL dict is
                 the ONE place to fix when Google changes its DOM.
  analyze.py   ← STABLE + TESTED. Raw JSON → LEADS / COMPETITORS / ALL csvs.
                 Auto-normalizes columns, so it ingests output from ANY engine
                 (this scraper, omkarcloud, gosom, Apify) unchanged.
  sectors.py   ← Your presets (driving-school, real-estate, gym, dentist…). A dict.
run.py         ← One command: scrape a sector in an area, then build the csvs.
data/raw/      ← raw JSON pulls (reproducible, one per search+area)
data/leads/    ← the deliverables (open in Excel/Sheets)
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

## Run it

```bash
# one sector, one area — scrape + analyze in one go
python run.py driving-school "Jayanagar, Bengaluru"

# bigger area, custom cap
python run.py real-estate "Whitefield, Bengaluru" --max 120
```

Output: `data/leads/driving-school-jayanagar-bengaluru-LEADS.csv` (+ `-COMPETITORS`, `-ALL`).

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

## Two constraints no tool can fix

- **Google caps ~120 results per search.** Scrape area-by-area, not one big query.
- **Scrape gently / headed** to avoid IP throttling. Keep volumes modest.

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
