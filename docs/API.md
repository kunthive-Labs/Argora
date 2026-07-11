# HTTP API — the local sidecar reference

Argora's FastAPI server (`webapp/server.py`, started by `app.py`) is
**localhost-only** — it drives a real browser on your machine and never runs in
the cloud. CORS allows any `http://localhost:*` / `http://127.0.0.1:*` origin,
which is how the KunthiveOS dev UI calls it directly as a sidecar.

Base URL: `http://127.0.0.1:8000` (change with `python app.py --port N`).
All request/response bodies are JSON unless noted. Errors come back as
FastAPI's standard `{"detail": "..."}` with the status codes listed.

**One job at a time.** A scrape or an extract opens a real browser, so the
server holds a single job slot — starting a second job returns `409` until the
first finishes or is stopped.

---

## Scrape jobs

### `POST /api/run` — start a scrape+analyze job

```jsonc
{
  "sectors":  ["driving-school"],          // preset keys (GET /api/sectors)
  "custom":   ["yoga studio"],             // free-text trades, scraped as-is
  "locations": ["Jayanagar, Bengaluru",    // batch: scraped in order,
                "HSR Layout, Bengaluru"],  //        paced by area_pause
  "location": "",                          // LEGACY single-area form — still
                                           // honoured when `locations` is empty
  "max": 120,                              // per-search cap (clamped 1–120)
  "headless": false,                       // headed is the default; keep it
  "min_reviews": null,                     // override every preset's review floor
  "area_pause": 180                        // seconds of rest between areas
}                                          // (clamped 30–3600)
```

Returns `{"job_id": "<id>"}` · `400` if no trade or no locality · `409` if a
job is already running.

At least one of `sectors`/`custom` and one of `locations`/`location` is
required. Every (trade × area) pair becomes one scrape target with its own
output files and summary row.

### `POST /api/stop` — ask the running job to stop

Returns `{"stopping": true|false}`. Graceful: the scraper finishes the card in
flight, partials are saved, CSVs are built with a `-RECOVERED` suffix. Also
aborts the rest between areas immediately.

### `GET /api/stream/{job_id}` — live progress (Server-Sent Events)

`text/event-stream`; each `data:` line is one JSON message:

| `kind` | Fields | Meaning |
|---|---|---|
| `log` | `line` | one log line (also kept for replay) |
| `phase` | `sector`, `area`, `areas`, `label` | a new (trade, area) target started — `label` is e.g. `area 2/3 · gym @ HSR Layout` |
| `summary` | `sector`, `location`, `scraped`, `leads`, `competitors`, `top`, `stem`, `error?` | one target finished; `error` present iff that target failed (the run continues) |
| `extract` | `folder`, `text_file`, `screenshots`, … | an extract job finished |
| `error` | `message` | the whole job failed (CAPTCHA wall, browser gone) |
| `done` | — | stream ends |

`: keep-alive` comment lines are sent every 15 s. If the job just finished (or
the client reconnects), the same URL **replays** the finished job's log +
summary + done. `404` for unknown ids.

### `GET /api/last-job` — the persisted outcome (survives restarts)

Returns `{"exists": false}` or the full job record:

```jsonc
{
  "exists": true,
  "job_id": "1783696861063-1",
  "kind": "scrape",                        // or "extract"
  "started_at": "2026-07-10T20:51:01",
  "finished_at": "2026-07-10T20:52:22",    // null while in_progress
  "in_progress": false,                    // true = server died mid-run;
                                           // this is the last checkpoint
  "ok": true,                              // no whole-job error (per-target
                                           // errors live on summary rows)
  "stopped": false,                        // user hit stop
  "error": null,
  "params": {"sectors": [], "custom": ["gyms"], "locations": ["HSR"],
             "max": 8, "area_pause": 180},
  "summary": [ /* summary rows, as in the SSE stream */ ],
  "log": [ /* last 500 log lines */ ]
}
```

The file is checkpointed after **every finished target**, so a killed server
still reports the progress it made (`in_progress: true`).

---

## Files & preview

### `GET /api/sectors`
`[{"key": "gym", "query": "gym", "min_reviews": 15}, …]` — the presets from
`leadfinder/sectors.py`, sorted.

### `GET /api/files`
`[{"name": "gym-hsr-LEADS.csv", "rows": 34, "kind": "LEADS"}, …]` — every CSV
in `data/leads/`; `kind` is `LEADS` / `COMPETITORS` / `ALL`.

### `GET /api/preview/{name}?limit=50`
`{"columns": [...], "rows": [...], "total": N}` — the CSV as JSON. `limit`
clamps to 1–5000 (the results browser asks for the full file). `404` if missing.

### `GET /api/download/{name}`
The raw file, from `data/leads/` or `data/sql/`. `404` if missing.

Filenames are always basenamed server-side — path traversal is a `404`/`400`,
never a file.

---

## Database push

### `GET /api/db-status`
`{"configured": bool, "reachable": bool, "origin"?: str, "leads_total"?: int, "detail"?: str}`
— is a KunthiveOS Postgres reachable? Never raises; `origin` is redacted (no
DSN ever reaches the browser). Drives the fol. 4 wax seal.

### `POST /api/sql` — generate the INSERT (no DB needed)
```jsonc
{"csv": "gym-hsr-LEADS.csv", "dataset": null, "include_has_site": false}
```
Returns `{"sql": "...", "count": N, "file": "gym-hsr.sql"}` and writes the file
under `data/sql/`. `dataset` defaults to `argora/<stem>`.

### `POST /api/push-db` — execute that same INSERT directly
Same body as `/api/sql`. Returns `{"inserted": N, "attempted": M, "origin": "..."}`.
`400` when no DB is configured · `502` on connection/SQL errors ·
idempotent by construction (see SCHEMA.md) — safe to re-run.

---

## Page extractor

### `POST /api/extract`
`{"url": "https://…", "mode": "auto", "headless": true}` — `mode` is
`auto | text | screenshot | both`. Returns `{"job_id"}` (shares the single job
slot; stream progress via `/api/stream/{id}`).

### `GET /api/extracts`
`[{"folder": "...", "files": [...], "screenshots": [...], "has_text": bool}, …]`

### `GET /api/extract-text/{folder}` → `{"folder", "text"}`
### `GET /api/extract-asset/{folder}/{name}` → the file (screenshot / text)

---

## Outreach

### `POST /api/outreach/queue` — the ranked worklist
```jsonc
{
  "csv": "gym-hsr-LEADS.csv",
  "limit": 200,
  "only_untouched": false,       // drop leads that already have a logged touch
  "sender": {"name": "…", "business": "…", "phone": "…", "signoff": "…"}
}
```
Returns `{"stem", "sector", "count", "competitors", "leads": [...]}` where each
lead carries its CSV fields plus:

- `lead_key` — `sql_gen.place_id_for()`, the SAME key used for the DB push, so
  touches line up with KunthiveOS rows
- `dataset` — `argora/<stem>`
- `last_touch` — the most recent logged touch, or `null`
- `messages` — ready-to-send copy for every channel:
  `{lead_key, variant, competitor|null, e164, has_phone,
    whatsapp: {text, url|null}, call: {tel|null, script},
    email: {subject, body, mailto}, walkin: {opening, leave_behind}}`
  — `variant` is `standard | upgrade | iconic`, picked from the lead's rank
  tags; the best same-category competitor (same PIN preferred, then most
  reviews) is cited inside the copy.

### `POST /api/outreach/log` — record a touch
```jsonc
{
  "lead_key": "0x…:0x…",           // required
  "name": "", "phone": "", "area": "", "category": "", "dataset": "",
  "channel": "whatsapp",           // whatsapp | call | email | walkin
  "outcome": "sent",               // sent | no_answer | callback | interested |
                                   // not_interested | converted
  "notes": "",
  "follow_up_at": null,            // ISO datetime or null
  "push_to_db": false              // also flip status/notes in KunthiveOS
}
```
Returns `{"touch": {...}, "pushed_to_db": bool, "writeback": {...}|null}`.
The local record is saved **even if the DB write-back fails** — an activity is
never lost to a network error.

### `GET /api/outreach/log?limit=200`
`{"touches": [most recent first], "total": N}`

### `GET /api/outreach/followups`
`{"due": [touches whose follow_up_at has passed and which have no later
touch]}` — sorted soonest first. Drives the follow-up banner/badge in the UI.

### `GET /api/outreach/route?csv=…-LEADS.csv`
`{"groups": [{"postal": "560102", "stops": [...]}, …], "total": N}` — the
walk-in route sheet, leads grouped by PIN code.

---

## Environment variables

| Var | Effect |
|---|---|
| `DATABASE_URL` | Postgres DSN for the DB push (first in the resolution order) |
| `KUNTHIVE_OS_DB_CONN` | path to a `.db-conn.json` (second) |
| `ARGORA_FAKE_SCRAPE=1` | scrape jobs return canned places — full-pipeline testing without touching Google |
| `OUTREACH_LLM=1` | enables the (currently no-op) LLM pitch-polish slot in `outreach.py` |

A `.env` file in the repo root is read at startup; real environment variables
always win over it.
