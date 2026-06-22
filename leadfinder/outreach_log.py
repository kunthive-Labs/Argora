"""
outreach_log.py — the lightweight local record of who you've reached out to.

This is deliberately NOT a CRM. KunthiveOS (Supabase) stays the system of record
for leads; this log only answers three operational questions Argora needs to run
outreach well:
  1. Have I already messaged this lead (so I don't double-message)?
  2. What follow-ups are due?
  3. What did I do recently?

It's a single JSON file under data/ — file-first like the rest of the app
(raw JSON, CSVs, .sql, extract folders), low-volume, single-writer (scrape/extract
jobs already serialise behind server._lock, and outreach writes are user clicks).
JSON keeps it trivially inspectable and avoids a second-source-of-truth feel.

The lead key is sql_gen.place_id_for(row) — the SAME key sql_gen/db use — so a
touch logged here lines up exactly with the row in KunthiveOS.
"""
import datetime as _dt
import json
import os
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(BASE, "data", "outreach")
LOG = os.path.join(DIR, "log.json")

CHANNELS = {"whatsapp", "call", "email", "walkin"}
OUTCOMES = {"sent", "no_answer", "interested", "not_interested",
            "callback", "converted"}


def _now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")


def load():
    """Return the whole log dict: {'version':1, 'touches':[...]}. Never raises."""
    if not os.path.exists(LOG):
        return {"version": 1, "touches": []}
    try:
        with open(LOG, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "touches" not in data:
            return {"version": 1, "touches": []}
        return data
    except Exception:
        return {"version": 1, "touches": []}


def _save(data):
    """Atomic write: temp file in the same dir, then os.replace."""
    os.makedirs(DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DIR, prefix=".log-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LOG)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def record(touch):
    """Append one touch (a dict). Fills in id + sent_at if absent, normalises
    channel/outcome, and returns the stored touch."""
    data = load()
    t = dict(touch)
    t.setdefault("sent_at", _now_iso())
    t["id"] = (data["touches"][-1]["id"] + 1) if data["touches"] else 1
    if t.get("channel") not in CHANNELS:
        t["channel"] = "whatsapp"
    if t.get("outcome") not in OUTCOMES:
        t["outcome"] = "sent"
    t.setdefault("notes", "")
    t.setdefault("follow_up_at", None)
    t.setdefault("pushed_to_db", False)
    data["touches"].append(t)
    _save(data)
    return t


def touched_keys():
    """{lead_key: [touches...]} — every lead that has at least one touch."""
    out = {}
    for t in load()["touches"]:
        out.setdefault(t.get("lead_key"), []).append(t)
    out.pop(None, None)
    return out


def last_touch(lead_key):
    """The most recent touch for a lead, or None."""
    ts = [t for t in load()["touches"] if t.get("lead_key") == lead_key]
    return max(ts, key=lambda t: t.get("sent_at", "")) if ts else None


def follow_ups_due(now=None):
    """Touches whose follow_up_at is set and <= now, where that lead hasn't had a
    LATER touch since (i.e. the follow-up is still outstanding). Sorted soonest
    first."""
    now = now or _now_iso()
    by_lead = touched_keys()
    due = []
    for t in load()["touches"]:
        fu = t.get("follow_up_at")
        if not fu or fu > now:
            continue
        later = [x for x in by_lead.get(t.get("lead_key"), [])
                 if x.get("sent_at", "") > t.get("sent_at", "")]
        if later:                       # already actioned after this follow-up
            continue
        due.append(t)
    due.sort(key=lambda t: t.get("follow_up_at") or "")
    return due
