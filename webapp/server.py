"""
webapp/server.py — local web UI for maps-lead-finder.

Run via ../app.py (which opens your browser). Endpoints:
  GET  /                     the single-page UI
  GET  /api/sectors          sector presets for the dropdown
  POST /api/run              start a scrape+analyze job  -> {job_id}
  POST /api/stop             ask the running job to stop early
  GET  /api/stream/{id}      Server-Sent Events: live log + summary
  GET  /api/files            list generated csv files (with row counts)
  GET  /api/preview/{name}   first rows of a csv as JSON
  GET  /api/download/{name}  download a csv / sql file
  POST /api/sql              generate Supabase INSERT sql from a csv

One job runs at a time (a scrape opens a real browser). Playwright's sync API
runs in a worker thread, never the asyncio loop thread.
"""
import json
import os
import queue
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from pydantic import BaseModel

from leadfinder import (analyze, db, extractor, outreach, outreach_log,
                        scraper, sectors, sql_gen)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")
LEADS = os.path.join(BASE, "data", "leads")
SQL = os.path.join(BASE, "data", "sql")
EXTRACTS = os.path.join(BASE, "data", "extracts")
OUTREACH = os.path.join(BASE, "data", "outreach")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
for d in (RAW, LEADS, SQL, EXTRACTS, OUTREACH):
    os.makedirs(d, exist_ok=True)

app = FastAPI(title="maps-lead-finder")

# KunthiveOS runs the Lead Finder UI locally (next dev on :3000) and calls this
# sidecar directly. Allow the local dev origins so the browser can reach us.
# Everything here is localhost-only; this server never runs in the cloud.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----- job state -------------------------------------------------------------
class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.q = queue.Queue()
        self.done = False
        self.stop = False
        self.summary = []  # per-sector dicts

    def emit(self, kind, **data):
        self.q.put({"kind": kind, **data})

    def log(self, line):
        self.emit("log", line=str(line))


_lock = threading.Lock()
_current = {"job": None}


def _slug(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _targets(sectors_sel, custom, min_reviews_override):
    """Build a uniform list of scrape targets from preset names + free-text
    custom queries. Each target is (key, query, exclude, min_reviews)."""
    targets = []
    for name in sectors_sel:
        preset = sectors.get(name)
        floor = preset["min_reviews"] if min_reviews_override is None else min_reviews_override
        targets.append((name, preset["query"], preset["exclude"], floor))
    for q in custom:
        q = q.strip()
        if not q:
            continue
        # a custom business type: no exclude list, review floor from the override
        # (default 0 — keep everything the search returns).
        targets.append((_slug(q), q, [], min_reviews_override or 0))
    return targets


def run_job(job, sectors_sel, custom, location, max_results, headless,
            min_reviews_override):
    try:
        for key, query, exclude, min_reviews in _targets(
                sectors_sel, custom, min_reviews_override):
            if job.stop:
                break
            name = key
            stem = f"{name}-{_slug(location)}"
            raw_path = os.path.join(RAW, f"{stem}.json")
            out_stem = os.path.join(LEADS, stem)

            job.emit("phase", sector=name, label=f"{name} @ {location}")
            job.log(f"▶ {query} in {location} (max {max_results}, ≥{min_reviews}★rev)")

            records = scraper.scrape(
                query, location, max_results, headless,
                log=job.log, should_stop=lambda: job.stop)

            with open(raw_path, "w") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            job.log(f"  raw saved → data/raw/{stem}.json ({len(records)} places)")

            allrec, leads, comps = analyze.analyze(
                records, exclude, min_reviews)
            analyze.write_csv(f"{out_stem}-ALL.csv", allrec)
            analyze.write_csv(f"{out_stem}-LEADS.csv", leads)
            analyze.write_csv(f"{out_stem}-COMPETITORS.csv", comps)

            s = {"sector": name, "location": location,
                 "scraped": len(allrec), "leads": len(leads),
                 "competitors": len(comps),
                 "top": (leads[0]["name"] + f" ({leads[0]['reviews']}★rev)") if leads else "—",
                 "stem": stem}
            job.summary.append(s)
            job.emit("summary", **s)
            job.log(f"  ✓ {len(leads)} leads · {len(comps)} competitors · "
                    f"{len(allrec)} total → data/leads/{stem}-LEADS.csv\n")
    except Exception as e:  # surface, don't crash the server
        job.log(f"✗ error: {e}")
        job.emit("error", message=str(e))
    finally:
        job.emit("done")
        job.done = True
        with _lock:
            _current["job"] = None


def run_extract_job(job, url, mode, headless):
    """Extract text (or full-page screenshots) from one exact URL."""
    try:
        res = extractor.extract(
            url, EXTRACTS, mode=mode, headless=headless,
            log=job.log, should_stop=lambda: job.stop)
        job.emit("extract", **res)
        kind = ("screenshot" if res["screenshots"] and not res["text_file"]
                else "text" if res["text_file"] else "empty")
        job.log(f"  ● done — {kind} · folder: {res['folder'] or '(none)'}")
    except Exception as e:
        job.log(f"✗ error: {e}")
        job.emit("error", message=str(e))
    finally:
        job.emit("done")
        job.done = True
        with _lock:
            _current["job"] = None


# ----- models ----------------------------------------------------------------
class RunReq(BaseModel):
    sectors: list[str] = []
    custom: list[str] = []          # free-text business types, scraped as-is
    location: str
    max: int = 120
    headless: bool = False
    min_reviews: int | None = None  # override the review floor for every target


class SqlReq(BaseModel):
    csv: str
    dataset: str | None = None
    include_has_site: bool = False


class PushReq(BaseModel):
    csv: str
    dataset: str | None = None
    include_has_site: bool = False


class ExtractReq(BaseModel):
    url: str
    mode: str = "auto"          # auto | text | screenshot | both
    headless: bool = True


class QueueReq(BaseModel):
    csv: str                            # a *-LEADS.csv from data/leads/
    limit: int = 200
    only_untouched: bool = False
    sender: dict | None = None          # {name, business, phone, signoff}


class TouchReq(BaseModel):
    lead_key: str
    name: str = ""
    phone: str = ""
    area: str = ""
    category: str = ""
    dataset: str = ""
    channel: str                        # whatsapp | call | email | walkin
    outcome: str = "sent"
    notes: str = ""
    follow_up_at: str | None = None     # ISO datetime, or null
    push_to_db: bool = False


# ----- routes ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/sectors")
def list_sectors():
    return [{"key": k, "query": v["query"], "min_reviews": v["min_reviews"]}
            for k, v in sorted(sectors.SECTORS.items())]


@app.post("/api/run")
def start_run(req: RunReq):
    with _lock:
        if _current["job"] and not _current["job"].done:
            raise HTTPException(409, "A job is already running.")
        custom = [c.strip() for c in req.custom if c.strip()]
        if not req.sectors and not custom:
            raise HTTPException(400, "Pick a preset or add a custom category.")
        if not req.location.strip():
            raise HTTPException(400, "Location is required.")
        job = Job(str(int(time.time() * 1000)))
        _current["job"] = job
    mr = None if req.min_reviews is None else max(0, req.min_reviews)
    t = threading.Thread(target=run_job, args=(
        job, req.sectors, custom, req.location.strip(),
        max(1, min(req.max, 120)), req.headless, mr), daemon=True)
    t.start()
    return {"job_id": job.id}


@app.post("/api/stop")
def stop_run():
    job = _current["job"]
    if job and not job.done:
        job.stop = True
        return {"stopping": True}
    return {"stopping": False}


@app.get("/api/stream/{job_id}")
def stream(job_id: str):
    job = _current["job"]
    if not job or job.id != job_id:
        raise HTTPException(404, "No such job (it may have finished).")

    def gen():
        while True:
            try:
                msg = job.q.get(timeout=15)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["kind"] == "done":
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"  # heartbeat
    return StreamingResponse(gen(), media_type="text/event-stream")


def _csv_rows(path):
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _competitors_for(leads_csv_name):
    """The COMPETITORS csv paired with a LEADS csv (same run). [] if missing."""
    comp = os.path.basename(leads_csv_name).replace(
        "-LEADS.csv", "-COMPETITORS.csv")
    path = os.path.join(LEADS, comp)
    return _csv_rows(path) if os.path.exists(path) else []


@app.get("/api/files")
def files():
    out = []
    for fn in sorted(os.listdir(LEADS)):
        if not fn.endswith(".csv"):
            continue
        path = os.path.join(LEADS, fn)
        try:
            n = sum(1 for _ in open(path)) - 1
        except Exception:
            n = 0
        kind = ("LEADS" if "-LEADS" in fn else
                "COMPETITORS" if "-COMPETITORS" in fn else "ALL")
        out.append({"name": fn, "rows": max(n, 0), "kind": kind})
    return out


@app.get("/api/preview/{name}")
def preview(name: str, limit: int = 50):
    path = os.path.join(LEADS, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    rows = _csv_rows(path)
    limit = max(1, min(limit, 5000))  # the results browser asks for the full file
    return {"columns": list(rows[0].keys()) if rows else [],
            "rows": rows[:limit], "total": len(rows)}


@app.get("/api/download/{name}")
def download(name: str):
    base = os.path.basename(name)
    for folder in (LEADS, SQL):
        path = os.path.join(folder, base)
        if os.path.exists(path):
            return FileResponse(path, filename=base)
    raise HTTPException(404, "Not found")


@app.post("/api/sql")
def gen_sql(req: SqlReq):
    path = os.path.join(LEADS, os.path.basename(req.csv))
    if not os.path.exists(path):
        raise HTTPException(404, "CSV not found")
    rows = sql_gen.load_csv(path)
    stem = os.path.basename(req.csv).replace("-LEADS.csv", "").replace(".csv", "")
    dataset = req.dataset or f"argora/{stem}"
    sql, n = sql_gen.generate(rows, dataset, only_leads=not req.include_has_site)
    out_name = stem + ".sql"
    with open(os.path.join(SQL, out_name), "w") as f:
        f.write(sql)
    return JSONResponse({"sql": sql, "count": n, "file": out_name})


@app.get("/api/db-status")
def db_status():
    """Is a KunthiveOS database reachable? Drives the 'Push to DB' affordance."""
    return db.status()


@app.post("/api/push-db")
def push_db(req: PushReq):
    """Execute the generated INSERT straight against the KunthiveOS Supabase —
    the no-copy-paste path. Same idempotent SQL as /api/sql, just run for you."""
    path = os.path.join(LEADS, os.path.basename(req.csv))
    if not os.path.exists(path):
        raise HTTPException(404, "CSV not found")
    stem = os.path.basename(req.csv).replace("-LEADS.csv", "").replace(".csv", "")
    dataset = req.dataset or f"argora/{stem}"
    try:
        result = db.push_csv(path, dataset, only_leads=not req.include_has_site)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # connection / SQL error — surface cleanly to the UI
        raise HTTPException(502, f"Push failed: {e}")
    return JSONResponse(result)


# ----- page extractor --------------------------------------------------------
@app.post("/api/extract")
def start_extract(req: ExtractReq):
    """Start a one-URL extraction job. Shares the single-browser job slot with
    scraping, and streams progress over the same /api/stream/{id} channel."""
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "A URL is required.")
    mode = req.mode if req.mode in ("auto", "text", "screenshot", "both") else "auto"
    with _lock:
        if _current["job"] and not _current["job"].done:
            raise HTTPException(409, "A job is already running.")
        job = Job(str(int(time.time() * 1000)))
        _current["job"] = job
    t = threading.Thread(target=run_extract_job,
                         args=(job, url, mode, req.headless), daemon=True)
    t.start()
    return {"job_id": job.id}


def _safe_under(root, *parts):
    """Resolve parts under root, refusing anything that escapes it."""
    path = os.path.realpath(os.path.join(root, *parts))
    if not path.startswith(os.path.realpath(root) + os.sep):
        raise HTTPException(400, "Bad path")
    return path


@app.get("/api/extracts")
def list_extracts():
    """List extraction folders with their files (text + screenshots)."""
    out = []
    if not os.path.isdir(EXTRACTS):
        return out
    for folder in sorted(os.listdir(EXTRACTS)):
        fpath = os.path.join(EXTRACTS, folder)
        if not os.path.isdir(fpath):
            continue
        files = sorted(f for f in os.listdir(fpath)
                       if os.path.isfile(os.path.join(fpath, f)))
        shots = [f for f in files if f.lower().endswith(".png")]
        has_text = "text.txt" in files
        out.append({"folder": folder, "files": files,
                    "screenshots": shots, "has_text": has_text})
    return out


@app.get("/api/extract-text/{folder}")
def extract_text(folder: str):
    path = _safe_under(EXTRACTS, os.path.basename(folder), "text.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "No text for this page.")
    with open(path, encoding="utf-8") as f:
        return {"folder": folder, "text": f.read()}


@app.get("/api/extract-asset/{folder}/{name}")
def extract_asset(folder: str, name: str):
    """Serve a screenshot or text file from an extraction folder (view/download)."""
    path = _safe_under(EXTRACTS, os.path.basename(folder), os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, filename=os.path.basename(name))


# ----- outreach studio -------------------------------------------------------
@app.post("/api/outreach/queue")
def outreach_queue(req: QueueReq):
    """Build a ranked worklist: each LEAD + its best-matched COMPETITOR turned
    into ready-to-send copy for every channel, annotated with the last touch."""
    path = os.path.join(LEADS, os.path.basename(req.csv))
    if not os.path.exists(path):
        raise HTTPException(404, "LEADS csv not found")
    leads = _csv_rows(path)
    comps = _competitors_for(req.csv)
    touched = outreach_log.touched_keys()
    stem = os.path.basename(req.csv).replace("-LEADS.csv", "").replace(".csv", "")
    sector = stem.split("-")[0]

    out = []
    for r in leads:
        comp = outreach.pick_competitor(r, comps)
        msgs = outreach.build_messages(r, comp, sector=sector, sender=req.sender)
        key = msgs["lead_key"]
        ts = touched.get(key) or []
        last = max(ts, key=lambda t: t.get("sent_at", "")) if ts else None
        if req.only_untouched and last:
            continue
        out.append({
            "lead_key": key,
            "name": r.get("name", ""), "phone": r.get("phone", ""),
            "category": r.get("category", ""), "area": r.get("area", "") or "",
            "address": r.get("address", ""),
            "rating": r.get("rating", ""), "reviews": r.get("reviews", ""),
            "score": r.get("score", ""), "tier": r.get("tier", ""),
            "rank_tags": r.get("rank_tags", ""),
            "maps_url": r.get("maps_url", ""),
            "messages": msgs, "last_touch": last,
            "dataset": f"argora/{stem}",
        })
        if len(out) >= max(1, req.limit):
            break
    return {"stem": stem, "sector": sector, "count": len(out),
            "competitors": len(comps), "leads": out}


@app.get("/api/outreach/log")
def outreach_log_list(limit: int = 200):
    touches = outreach_log.load()["touches"]
    recent = sorted(touches, key=lambda t: t.get("sent_at", ""),
                    reverse=True)[:max(1, limit)]
    return {"touches": recent, "total": len(touches)}


@app.post("/api/outreach/log")
def outreach_log_add(req: TouchReq):
    """Record one outreach touch locally; optionally also flip the lead's
    status/notes in KunthiveOS. The local record is saved even if the optional
    write-back fails — we never lose the activity."""
    pushed, writeback = False, None
    if req.push_to_db:
        try:
            writeback = db.log_outreach(
                req.lead_key, outcome=req.outcome,
                channel=req.channel, note=req.notes)
            pushed = bool(writeback.get("written"))
        except Exception as e:                  # never 500 the local write
            writeback = {"written": False, "reason": str(e)}

    payload = req.model_dump(exclude={"push_to_db"})
    payload["pushed_to_db"] = pushed
    touch = outreach_log.record(payload)
    return {"touch": touch, "pushed_to_db": pushed, "writeback": writeback}


@app.get("/api/outreach/followups")
def outreach_followups():
    return {"due": outreach_log.follow_ups_due()}


@app.get("/api/outreach/route")
def outreach_route(csv: str):
    """Walk-in route sheet: leads grouped by postal code (then address order)."""
    path = os.path.join(LEADS, os.path.basename(csv))
    if not os.path.exists(path):
        raise HTTPException(404, "LEADS csv not found")
    groups = {}
    for r in _csv_rows(path):
        pin = sql_gen.extract_postal(r.get("address", "")) or "—"
        groups.setdefault(pin, []).append({
            "name": r.get("name", ""), "phone": r.get("phone", ""),
            "address": r.get("address", ""), "maps_url": r.get("maps_url", ""),
            "reviews": r.get("reviews", ""), "tier": r.get("tier", ""),
        })
    out = [{"postal": k, "stops": v} for k, v in sorted(groups.items())]
    return {"groups": out, "total": sum(len(g["stops"]) for g in out)}
