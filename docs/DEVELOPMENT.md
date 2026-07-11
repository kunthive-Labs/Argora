# Development guide

For anyone changing the code. Read the README first for what the app *does*;
this covers how it's put together and how to work on it safely.

## The one architectural rule

**The scraper is the only fragile layer.** Google rewrites its Maps DOM every
few months; when that happens, `leadfinder/scraper.py` breaks and *nothing
else does*. Everything downstream (analyze → ranking → sql_gen → outreach)
consumes plain dicts/CSVs and is pure, deterministic, and tested. Keep it that
way: never let Playwright, network calls, or selectors leak out of
`scraper.py` / `extractor.py`.

## Module map

| Module | Stability | Role |
|---|---|---|
| `leadfinder/scraper.py` | **fragile** | Drives Google Maps. `SEL` dict = the only thing to fix on DOM rot. CAPTCHA detection (`is_captcha_page`), one retry per detail page, per-place `checkpoint` callback, `ScrapeError(results, fatal)`. |
| `leadfinder/extractor.py` | fragile | One-URL page capture (text, else full-page screenshots). |
| `leadfinder/analyze.py` | stable, tested | Raw JSON (from ANY engine — column aliases) → normalize, dedupe, rank → LEADS/COMPETITORS/ALL CSVs. |
| `leadfinder/ranking.py` | stable, tested | The 5-dimension 0–100 score, tiers, tags. **Mirrors `KunthiveOS/lib/scoring.ts`** — see "Ranking parity" below. Inputs are defensively coerced (strings/None/junk degrade, never raise); the scoring math itself must not drift. |
| `leadfinder/sql_gen.py` | stable, tested | CSV → idempotent Supabase INSERT. Owns `place_id_for()` (the universal lead key), `norm_phone()`, `stem_of()`. |
| `leadfinder/db.py` | stable | Executes sql_gen's SQL against KunthiveOS Postgres (deliberately NOT parameterized — the .sql file and the direct push must stay byte-identical; see the module docstring). Outreach write-back. DSN resolution. |
| `leadfinder/outreach.py` | stable, tested | Pure pitch templating: lead + competitor → WhatsApp/call/email/walk-in copy. Message templates are module-level format strings — hand-edit freely. |
| `leadfinder/outreach_log.py` | stable, tested | The local JSON touch log (not a CRM). `follow_ups_due()`. |
| `leadfinder/sectors.py` | config | Trade presets: `query`, `exclude`, `min_reviews`. Add yours here. |
| `webapp/server.py` | tested | FastAPI. Single job slot (`_acquire_job`), the area×trade run loop (`run_job` → `_scrape_one`), SSE streaming, job checkpointing, all HTTP endpoints (docs/API.md). |
| `webapp/static/index.html` | — | The whole UI, **one file, no build step** — a deliberate constraint. Plain DOM + fetch + EventSource. |

Data flows one way: `scraper → data/raw/*.json → analyze(+ranking) →
data/leads/*.csv → {sql_gen → data/sql | db.push | outreach}`. Every stage's
input/output is a file you can inspect.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest                       # 200+ cases, < 1s, no browser, no network
```

Conventions the suite follows (keep them):

- **Pure parts only.** Nothing imports Playwright or opens a socket. The
  scraper's testable helpers (`is_captcha_page`, `field_yield`, `save_json`,
  CLI `main` with a monkeypatched `scrape`) are covered; the live loop is not.
- **`test_server.py` drives functions directly** — `run_job` with a
  monkeypatched `scraper.scrape`, dirs pointed at `tmp_path`. There's no
  `TestClient` (httpx isn't a dependency); don't add one casually.
- Boundary tables in the ranking tests pin the spec — if you change a
  boundary, you're changing the *algorithm*, and the TS twin + the handover
  doc must change with it.

## Manual testing without Google

```bash
ARGORA_FAKE_SCRAPE=1 python app.py
```

Scrape jobs return 8 canned places per target (a mix of NO-SITE and has-site)
with realistic log pacing — SSE streaming, area pacing/rests, Halt, checkpoint
writes, CSVs, the outreach studio, everything works end-to-end. This is how to
verify UI/server changes without burning real scrapes.

## When the scrape starts returning blanks (selector rot)

The field-yield report tells you. After every scrape you'll see:

```
field yield: name 100%, rating 96%, reviews 96%, category 91%, address 98%, phone 74%
```

A sudden drop in one field (with a `!! LOW YIELD` warning) means that field's
selector went stale. Fix the matching entry in the `SEL` dict at the top of
`leadfinder/scraper.py` — open Maps in a normal browser, inspect the element,
update the selector. That dict is the *only* place to touch. `website` is
deliberately excluded from the warnings: low website yield is the product
working.

## Ranking parity with KunthiveOS

`ranking.py` and `KunthiveOS/lib/scoring.ts` implement the same spec
(`KunthiveOS/docs/handover-lead-ranking-algorithm.md`). Rules:

- **Coercion is Python-side armor** (CSV strings, None, junk → neutral) and
  may evolve freely.
- **Scores, boundaries, tags, tiers, disqualification, and the cap** must stay
  identical to the TS twin. Change them only alongside the TS file + the
  handover doc, and update the boundary tests in `tests/test_ranking.py`.

## Common changes, where to make them

| I want to… | Touch |
|---|---|
| add a trade preset | `leadfinder/sectors.py` (copy a block) |
| reword the pitch copy | the template dicts in `leadfinder/outreach.py` |
| add a premium PIN zone | `PREMIUM_PINS` in `ranking.py` **and** the TS twin |
| classify another site-builder as "no real website" | `SOCIAL_BUILDER`/`GOOGLE_SITES` in `ranking.py` **and** the TS twin |
| accept another scraper's JSON | `FIELD_ALIASES` in `analyze.py` |
| add an endpoint | `webapp/server.py` + document it in `docs/API.md` |
| change UI | `webapp/static/index.html` — keep it one file, no build step |

## Hard boundaries (do not cross)

- **No anti-bot evasion.** No CAPTCHA solving, proxy/IP rotation, fingerprint
  randomization, or login automation — detection + graceful stop only. This is
  a project constraint, not a style preference. See BARRIERS.md §2.
- **Argora never becomes a second lead store.** KunthiveOS is the system of
  record; the outreach log answers "have I messaged them / what's due", nothing
  more. The DB path only INSERTs new leads and only UPDATEs status/notes of
  existing ones.
- **`data/` never enters git.** It's business PII. The `.gitkeep` files keep
  the folders; the contents stay local.
