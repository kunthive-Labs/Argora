"""
db.py — push generated leads straight into the KunthiveOS Supabase.

This is the "no copy-paste" path. `sql_gen.generate()` already builds the exact
idempotent INSERT (INSERT-only, skip-by-place_id-OR-phone, ON CONFLICT DO
NOTHING). Here we just *execute* that same SQL against Postgres instead of
writing it to a .sql file — so the dedup guarantees are byte-for-byte identical
whether you paste the SQL in the Supabase editor or click "Push" in the UI.

DSN resolution, in order:
  1. $DATABASE_URL                         (an explicit postgres URL)
  2. $KUNTHIVE_OS_DB_CONN                   (path to a .db-conn.json)
  3. ../KunthiveOS/.db-conn.json            (the sibling repo's local creds)
  4. ~/Documents/Beta/KunthiveOS/.db-conn.json

The .db-conn.json is the same file KunthiveOS's own import scripts read — a
direct-Postgres connection ({host,port,user,password,database}). It's git-ignored
on the KunthiveOS side, so creds never travel through this repo.
"""
import json
import os
import urllib.parse

from leadfinder import sql_gen

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file():
    """Minimal .env reader (no python-dotenv dep). Existing env wins, so a real
    export always overrides the file. Called once at import."""
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except Exception:
        pass  # a broken .env shouldn't take the server down


_load_env_file()


def _conn_json_candidates():
    explicit = os.environ.get("KUNTHIVE_OS_DB_CONN")
    if explicit:
        yield explicit
    yield os.path.join(BASE, "..", "KunthiveOS", ".db-conn.json")
    yield os.path.expanduser("~/Documents/Beta/KunthiveOS/.db-conn.json")


def _dsn_from_conn_json(path):
    with open(path) as f:
        c = json.load(f)
    # URL-encode user + password: Supabase passwords routinely contain @ / : ? #
    # which otherwise corrupt the DSN (the userinfo bleeds into the host/query).
    user = urllib.parse.quote(str(c["user"]), safe="")
    pw = urllib.parse.quote(str(c["password"]), safe="")
    host = c["host"]
    port = c.get("port", 5432)
    db = c.get("database", "postgres")
    # Supabase requires TLS; sslmode=require avoids cert-chain hassles locally.
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}?sslmode=require"


def resolve_dsn():
    """Return (dsn, origin) or (None, reason) if nothing is configured."""
    env = os.environ.get("DATABASE_URL")
    if env:
        return env, "env:DATABASE_URL"
    for path in _conn_json_candidates():
        if path and os.path.exists(path):
            try:
                return _dsn_from_conn_json(path), path
            except Exception as e:  # malformed file — say so, don't crash
                return None, f"unreadable {path}: {e}"
    return None, "no DATABASE_URL and no KunthiveOS/.db-conn.json found"


def status():
    """Lightweight reachability probe for the UI. Never raises."""
    dsn, origin = resolve_dsn()
    if not dsn:
        return {"configured": False, "reachable": False, "detail": origin}
    try:
        import psycopg
    except ImportError:
        return {"configured": True, "reachable": False, "origin": _redact(origin),
                "detail": "psycopg not installed — pip install -r requirements.txt"}
    try:
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from leads")
                total = cur.fetchone()[0]
        return {"configured": True, "reachable": True, "origin": _redact(origin),
                "leads_total": total}
    except Exception as e:
        return {"configured": True, "reachable": False, "origin": _redact(origin),
                "detail": str(e)}


def push_csv(csv_path, dataset, only_leads=True):
    """Generate the INSERT from a leads CSV and execute it. Returns a dict with
    `inserted` (rows that were new) and `attempted` (candidates after dedup)."""
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is required to push to the DB — `pip install -r requirements.txt`"
        ) from e

    dsn, origin = resolve_dsn()
    if not dsn:
        raise RuntimeError(f"No database configured: {origin}")

    rows = sql_gen.load_csv(csv_path)
    sql, attempted = sql_gen.generate(rows, dataset, only_leads=only_leads)
    if attempted == 0:
        return {"inserted": 0, "attempted": 0, "origin": _redact(origin),
                "note": "no new candidate rows in this file"}

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            inserted = cur.rowcount  # INSERT-only → rowcount is exactly the new rows
        conn.commit()
    return {"inserted": max(inserted, 0), "attempted": attempted,
            "origin": _redact(origin)}


# outcome (from the outreach log) -> KunthiveOS lead_status enum value
_OUTCOME_STATUS = {
    "sent": "contacted", "no_answer": "contacted", "callback": "contacted",
    "interested": "interested", "converted": "converted",
    "not_interested": "disqualified",
}
# outreach channel -> activities.channel enum value
_ACTIVITY_CHANNEL = {
    "whatsapp": "whatsapp", "call": "call", "email": "email", "walkin": "meeting",
}


def log_outreach(lead_key, *, outcome="sent", channel="whatsapp", note=""):
    """OPTIONAL, non-breaking write-back: move an existing lead's status and
    append a note in the KunthiveOS `leads` table, and record an `activities`
    row. Matched by source_place_id = lead_key (the same key sql_gen synthesises).

    Never inserts a lead — that's push_csv's job — so Argora never becomes a
    competing lead store. Returns {written: bool, ...}; raises only on a true
    connection error (the caller wraps it so the local log is never lost)."""
    dsn, _ = resolve_dsn()
    if not dsn:
        return {"written": False, "reason": "no database configured"}
    try:
        import psycopg
    except ImportError:
        return {"written": False, "reason": "psycopg not installed"}

    status = _OUTCOME_STATUS.get(outcome, "contacted")
    note_line = f"[argora/{channel}] {outcome}" + (f": {note}" if note else "")

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads "
                "SET status = %s::lead_status, "
                "    notes = trim(coalesce(notes,'') || E'\\n' || %s), "
                "    updated_at = now() "
                "WHERE source_place_id = %s "
                "RETURNING id",
                (status, note_line, lead_key))
            row = cur.fetchone()
            if not row:
                return {"written": False,
                        "reason": "lead not in KunthiveOS yet — push it first"}
            lead_id = row[0]
        conn.commit()                       # the status update is now durable

        # An activity row is a nice-to-have. Run it in its OWN transaction so a
        # missing/locked activities table (which would abort the transaction in
        # Postgres) can never undo the status update above.
        try:
            with conn.cursor() as cur:
                ch = _ACTIVITY_CHANNEL.get(channel, "system")
                cur.execute(
                    "INSERT INTO activities "
                    "(entity_type, entity_id, type, channel, summary) "
                    "VALUES ('lead', %s, 'note', %s::activity_channel, %s)",
                    (lead_id, ch, note_line))
            conn.commit()
        except Exception:
            conn.rollback()
    return {"written": True, "status": status, "lead_id": lead_id}


def _redact(origin):
    """Never echo a full DSN back to the browser."""
    if isinstance(origin, str) and origin.startswith("env:"):
        return origin
    if isinstance(origin, str) and origin.endswith(".json"):
        return os.path.basename(os.path.dirname(origin)) + "/" + os.path.basename(origin)
    return "configured"
