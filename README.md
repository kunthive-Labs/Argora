# Argora — Ledger of Prospects

Free Google-Maps lead-gen for **no-website prospecting**, with a full outreach
workflow on top. Argora finds local businesses that have **no real website**
while their same-category competitors do, ranks them by pitch-conversion
probability, pushes them into the **KunthiveOS** database, and then turns each
lead into a **ready-to-send, multi-channel pitch** (WhatsApp / call / email /
walk-in) — so the whole client-acquisition funnel runs from one screen.

No Apify credits, no Google Places API. A maintained-style Playwright scraper
does the extraction; the value lives in the **lead-intelligence + outreach
layer** on top (sector presets, website split, ranking, competitor matching,
message generation, an outreach log).

---

## The funnel, end to end

```
1. DISCOVER   scrape Google Maps for a trade in an area        (fol. 1–2)
2. RANK       5-dimension scoring → LEADS / COMPETITORS / ALL  (fol. 3)
3. PUSH       idempotent INSERT into KunthiveOS `leads`         (fol. 4)
4. ENRICH     transcribe a competitor's page (pricing, etc.)   (fol. 5)
5. OUTREACH   per-lead pitch + log the touch + follow-ups       (fol. 6)
```

KunthiveOS (the sibling Supabase project) stays the **system of record** for
leads. Argora is the cockpit that fills it and works it.

---

## Architecture — why it's split this way

```
app.py             ← THE one command — launches the web app + opens your browser
run.py             ← CLI alternative: scrape a sector in an area, build the csvs
webapp/
  server.py        ← FastAPI backend: runs jobs, streams live logs (SSE),
                     SQL/DB API, page-extractor API, outreach API
  static/
    index.html     ← the single-file UI (no build step) — six "folios"
leadfinder/
  scraper.py       ← FRAGILE. Drives Google Maps, pulls raw places. The SEL dict
                     is the ONE place to fix when Google changes its DOM.
  analyze.py       ← STABLE + TESTED. Raw JSON → LEADS / COMPETITORS / ALL csvs.
                     Auto-normalizes columns, so it ingests output from ANY engine.
  ranking.py       ← STABLE + TESTED. The 5-dimension lead-scoring algorithm
                     (score 0–100, tier hot/warm/cool/cold, tags). Single source
                     of truth; mirrors KunthiveOS/lib/scoring.ts.
  sql_gen.py       ← STABLE + TESTED. csv → Supabase `leads` INSERT sql (idempotent).
  db.py            ← Direct "no copy-paste" push to the KunthiveOS Postgres, plus
                     the optional outreach write-back (status/notes/activities).
  extractor.py     ← Single-URL page capture (text, else full-page screenshot).
  outreach.py      ← Pitch templating: lead + competitor → WhatsApp/call/email/
                     walk-in copy. Pure functions, no network (optional LLM slot).
  outreach_log.py  ← The lightweight local outreach log (JSON). NOT a CRM.
  sectors.py       ← Your trade presets (driving-school, real-estate, gym, law…).
data/raw/          ← raw JSON pulls               (git-ignored)
data/leads/        ← the deliverables — csv        (git-ignored)
data/sql/          ← generated Supabase sql        (git-ignored)
data/extracts/     ← transcribed competitor pages  (git-ignored)
data/outreach/     ← the outreach log (PII)         (git-ignored)
```

The split is deliberate: the scraper is a maintenance treadmill (selectors rot,
IPs throttle). The analysis + outreach layers — your real moat — never break
when Google changes its markup.

---

## Prerequisites

- **Python 3.10+** (the repo's venv was built with 3.14; anything 3.10+ is fine).
- **git** with access to this repo (`kunthive-Labs/Argora`).
- For the DB push / outreach write-back (optional): the **KunthiveOS** repo
  checked out as a sibling, or a Postgres connection string. See
  *"Connect the KunthiveOS database"* below.

---

## Fresh-system setup (do this once on the new machine)

```bash
# 1. Clone
git clone git@github.com:kunthive-Labs/Argora.git maps-lead-finder
cd maps-lead-finder

# 2. Create + activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install the Chromium browser Playwright drives
playwright install chromium
```

> **Two things do NOT come through git** (they're git-ignored on purpose):
> 1. **Your scraped data** in `data/` — it's local business PII. You start the
>    new machine with empty `data/` folders and re-scrape (or copy the `data/`
>    folder across by hand if you want your history).
> 2. **Database credentials** — see the next section. You must reconnect the DB
>    on the new machine before "Push to DB" / outreach write-back will work.

### Connect the KunthiveOS database (optional but recommended)

The DB push and the outreach write-back need a Postgres connection. Argora looks
for it in this order — set up **any one**:

1. **`DATABASE_URL`** env var (or a `.env` file in the repo root):
   ```bash
   # .env  — copy from .env.example, never committed
   DATABASE_URL=postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres?sslmode=require
   ```
   (Supabase → Project Settings → Database → Connection string. Special
   characters in the password are handled automatically.)

2. **The KunthiveOS sibling repo** — if you also clone KunthiveOS next to this
   repo, Argora auto-reads its local `../KunthiveOS/.db-conn.json`. Nothing to
   configure.

3. **`KUNTHIVE_OS_DB_CONN`** env var pointing at a `.db-conn.json` elsewhere.

Run **once** against the database before pushing Argora leads: apply KunthiveOS
migration `0010_leads_v2_rich_fields.sql` (adds the `'argora'` source value and
the rich columns). See **SCHEMA.md** for the full column mapping.

You can verify the connection from the UI (fol. 4, the wax seal turns green) or:
```bash
python -c "from leadfinder import db; print(db.status())"
```

---

## Run it — the web app (recommended)

```bash
source .venv/bin/activate
python app.py                      # → http://127.0.0.1:8000 opens automatically
# options:
python app.py --port 9000 --no-open
```

`Ctrl-C` stops the server. **One job runs at a time** (a scrape/extract opens a
real browser).

### The six folios

| Folio | Name | What you do |
|---|---|---|
| **fol. 1** | Open an entry | Pick trades (preset chips or write your own) + a locality, set max/search. **Post the run.** |
| **fol. 2** | Posting | Watch the scrape **live** — each place streams in, with running lead/competitor/scraped counts. |
| **fol. 3** | The ledger | Browse/sort/filter any generated CSV; click a row for full detail; download. |
| **fol. 4** | Post to the master | Pick a LEADS book → **Draft the SQL** (copy/download) or **Post direct** to KunthiveOS (idempotent, safe to re-run). |
| **fol. 5** | Page extractor | Paste one exact URL → transcribe its text (or a full-page screenshot if it can't be copied). Use it to grab a competitor's pricing/services page. |
| **fol. 6** | Outreach studio | Open a LEADS book → each lead becomes a ready-to-send pitch. Fire WhatsApp/Call/Email/walk-in, log the touch, set follow-ups. |

---

## Run it — the CLI (scriptable)

```bash
# one sector, one area — scrape + analyze in one go
python run.py driving-school "Jayanagar, Bengaluru"

# bigger area, custom cap
python run.py real-estate "Whitefield, Bengaluru" --max 120
```

Output: `data/leads/driving-school-jayanagar-bengaluru-LEADS.csv`
(+ `-COMPETITORS`, `-ALL`).

Other CLI entry points:
```bash
# generate Supabase SQL from a LEADS csv
python -m leadfinder.sql_gen data/leads/gym-yelahanka-LEADS.csv \
    --dataset argora/gym-yelahanka --out data/sql/gym-yelahanka.sql

# merge several area pulls into one deduped list
python -m leadfinder.scraper "real estate agent" "HSR Layout, Bengaluru" --out data/raw/re-hsr.json
python -m leadfinder.analyze "data/raw/re-*.json" --out data/leads/real-estate --sector real-estate

# transcribe one page
python -m leadfinder.extractor "https://example.com/pricing" --out data/extracts --mode auto
```

---

## The daily operating workflow

1. **fol. 1–2** — scrape one trade in one neighbourhood (headed, modest — see
   BARRIERS.md). Repeat for a few areas across a session, spaced out.
2. **fol. 4** — push the LEADS book into KunthiveOS (idempotent; never
   duplicates).
3. **fol. 6 — Outreach studio:**
   - Fill **Your details** once (name / business / phone) — it's kept in your
     browser and signs every message.
   - Pick the LEADS book, **Open work**. Leads come ranked, each with a pitch
     that already names the competitor who outranks them.
   - For each lead: hit **WhatsApp** (opens wa.me with the message pre-filled),
     **Call** (dials + a script you can expand), **Email** (paste an address,
     opens your mail client), or copy the text. Walk-in stops are grouped by PIN
     in the side panel (printable).
   - Set the **outcome** (sent / no-answer / interested / callback / converted),
     add a note, optionally a **+2d / +1w follow-up**, and **Log touch**.
   - Tick **push** to also flip the lead's status/notes in KunthiveOS (only when
     the DB is reachable).
   - **Untouched only** hides leads you've already worked; **Follow-ups due**
     resurfaces the ones to chase.

The outreach log lives in `data/outreach/log.json` (local, git-ignored). It
stops you double-messaging and tracks follow-ups — it is **not** a second CRM;
KunthiveOS remains the system of record.

---

## The three output files (per scrape)

| File | Rows | Use |
|---|---|---|
| `…-LEADS.csv` | no real website + has phone, ranked | **who you pitch** |
| `…-COMPETITORS.csv` | same trade, HAS a website, ranked | pitch ammo + the competitor each lead is cited against |
| `…-ALL.csv` | everything, with `website_status` | the full picture / QA |

**Ranking** is a 5-dimension score (0–100): web gap (40) · review volume (25) ·
rating trust (15) · reachability (10) · category urgency (10), plus
bonuses/tags (upgrade_pitch, iconic_local, premium_zone…) and a tier
(hot/warm/cool/cold). Google Sites / `business.site` auto-pages count as **no
website** — the strongest kind of lead. Full spec:
`KunthiveOS/docs/handover-lead-ranking-algorithm.md`.

---

## Before you scale — read BARRIERS.md

`BARRIERS.md` is the must-read: what gets you throttled/banned and what never to
build. Short version:

- **Google caps ~120 results per search.** Scrape area-by-area, not one big query.
- **Headed + gentle + spaced-out.** Headless at volume is the easiest bot tell.
- **No evasion tooling** (CAPTCHA solvers, proxy rotation, login automation).
- **Logged out**, always. Never drive Maps signed into your Google account.
- **Data stays local** — `data/` is git-ignored; it's business PII.
- **TRAI/DND**: outreach is low-volume, relevant, legit — no bulk auto-dialers/spam.
- **Selectors rot** — when extraction goes blank, fix the `SEL` dict in `scraper.py`.

---

## Adding a sector

Edit `leadfinder/sectors.py` — copy a block, change `query`, `exclude`
(keywords that mean "wrong business"), and `min_reviews`. That's it. Or just
type a free-text trade in fol. 1.

---

## Pushing to Supabase — the safety guarantee

The generated INSERT **only ever INSERTs, and can never add anything twice** —
guarded four ways (intra-batch dedup, `WHERE NOT EXISTS` on place-id *or* phone,
`ON CONFLICT` on the unique constraint, INSERT-only). Verified end-to-end against
Postgres 16. Full mapping + guarantees: **SCHEMA.md**.

---

## Relationship to `../maps-lead-gen`

`maps-lead-gen` is the **Apify-based** version (pay-per-event,
`compass/crawler-google-places`). This repo is the **free Playwright** version.
`analyze.py` here is column-compatible with that repo's Apify JSON, so you can
mix sources.
