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
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from pydantic import BaseModel

from leadfinder import analyze, scraper, sectors, sql_gen

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")
LEADS = os.path.join(BASE, "data", "leads")
SQL = os.path.join(BASE, "data", "sql")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
for d in (RAW, LEADS, SQL):
    os.makedirs(d, exist_ok=True)

app = FastAPI(title="maps-lead-finder")


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


def run_job(job, sectors_sel, location, max_results, headless):
    try:
        for name in sectors_sel:
            if job.stop:
                break
            preset = sectors.get(name)
            stem = f"{name}-{_slug(location)}"
            raw_path = os.path.join(RAW, f"{stem}.json")
            out_stem = os.path.join(LEADS, stem)

            job.emit("phase", sector=name, label=f"{name} @ {location}")
            job.log(f"▶ {preset['query']} in {location} (max {max_results})")

            records = scraper.scrape(
                preset["query"], location, max_results, headless,
                log=job.log, should_stop=lambda: job.stop)

            with open(raw_path, "w") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            job.log(f"  raw saved → data/raw/{stem}.json ({len(records)} places)")

            allrec, leads, comps = analyze.analyze(
                records, preset["exclude"], preset["min_reviews"])
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


# ----- models ----------------------------------------------------------------
class RunReq(BaseModel):
    sectors: list[str]
    location: str
    max: int = 120
    headless: bool = False


class SqlReq(BaseModel):
    csv: str
    dataset: str | None = None
    include_has_site: bool = False


# ----- routes ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html")) as f:
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
        if not req.sectors:
            raise HTTPException(400, "Pick at least one category.")
        if not req.location.strip():
            raise HTTPException(400, "Location is required.")
        job = Job(str(int(time.time() * 1000)))
        _current["job"] = job
    t = threading.Thread(target=run_job, args=(
        job, req.sectors, req.location.strip(),
        max(1, min(req.max, 120)), req.headless), daemon=True)
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
def preview(name: str):
    path = os.path.join(LEADS, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    rows = _csv_rows(path)
    return {"columns": list(rows[0].keys()) if rows else [],
            "rows": rows[:50], "total": len(rows)}


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
