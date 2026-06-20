"""
sql_gen.py — turn a LEADS/ALL csv into Supabase INSERT statements for KunthiveOS.

Targets the real `leads` table (supabase/migrations/0001_init.sql):

  leads(name, phone, area, postal_code,
        rating numeric(2,1), reviews_count int, maps_link,
        website_url, website_status website_status,   -- enum: none|social|real
        category, source lead_source,                 -- enum: ...|import
        source_dataset, source_place_id text UNIQUE,   -- the dedup key
        score int, ...defaults...)

SAFETY (what stops anything being added twice, even by mistake):
  1. Intra-batch dedup in Python — by place_id AND by normalized phone.
  2. WHERE NOT EXISTS guard against the live table — skips a row if its
     source_place_id OR its normalized phone already exists. This covers the
     case where the same business is already in the DB under a *different*
     place-id format (old Apify `ChIJ...` vs this scraper's `0x...` hex).
  3. ON CONFLICT (source_place_id) DO NOTHING — final backstop on the UNIQUE
     constraint, so even a race or a repeated id can never double-insert.
  4. We only ever INSERT — never UPDATE/DELETE — so existing rows are untouched.

  source_place_id is NEVER null (we synthesise `argora:<hash>` from name+phone
  when the URL has no id), so the unique constraint always bites.

Enum mapping (v2 — needs migration 0010 applied first):
  source         -> 'argora'   (lead_source value added in 0010)
  website_status -> 'none' (no site) | 'social' (social/builder only) | 'real'

v2 rich columns (migration 0010): full_address, latitude, longitude,
  social_links (jsonb, e.g. {"facebook": "..."}), raw (jsonb catch-all).

Usage:
    python -m leadfinder.sql_gen data/leads/gym-yelahanka-LEADS.csv \\
        --dataset argora/gym-yelahanka --out data/sql/gym-yelahanka.sql
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import urllib.parse

from leadfinder import analyze

TABLE = "leads"
# order is fixed and matches both the INSERT column list and the VALUES alias
COLUMNS = ["name", "phone", "area", "postal_code", "full_address",
           "latitude", "longitude", "rating", "reviews_count", "maps_link",
           "website_url", "website_status", "social_links", "category",
           "source", "source_dataset", "source_place_id",
           "score", "tier", "rank_tags", "score_breakdown", "raw"]

# valid enum values — guard rails so we never emit garbage
LEAD_SOURCE = "argora"            # lead_source enum (added in migration 0010)
WEBSITE_STATUS = {"none", "social", "real"}

# social / builder / directory host -> platform key for the social_links jsonb
SOCIAL_PLATFORMS = [
    ("facebook.com", "facebook"), ("fb.com", "facebook"),
    ("instagram.com", "instagram"), ("instagr.am", "instagram"),
    ("twitter.com", "twitter"), ("x.com", "twitter"),
    ("linkedin.com", "linkedin"), ("youtube.com", "youtube"),
    ("wa.me", "whatsapp"), ("whatsapp.com", "whatsapp"),
    ("linktr.ee", "linktree"), ("justdial.com", "justdial"),
    ("indiamart.com", "indiamart"), ("sulekha.com", "sulekha"),
    ("business.site", "google_business"), ("g.page", "google_business"),
]


# ---- field derivation -------------------------------------------------------
def extract_place_id(maps_url):
    if not maps_url:
        return ""
    m = re.search(r"query_place_id=([^&]+)", maps_url)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", maps_url)
    if m:
        return m.group(1)
    return ""


def synth_place_id(name, phone):
    h = hashlib.sha1(f"{name}|{phone}".encode()).hexdigest()[:16]
    return f"argora:{h}"


def place_id_for(row):
    return (extract_place_id(row.get("maps_url", ""))
            or synth_place_id(row.get("name", ""), row.get("phone", "")))


def norm_phone(phone):
    """Digits only — so '+91 98860 11111' and '9886011111' compare equal."""
    return re.sub(r"\D", "", phone or "")


def classify_website(website):
    """Return (website_status_enum, website_url) for the leads table."""
    w = (website or "").strip()
    if not w or w.lower() in ("none", "n/a", "na", "-", "null"):
        return "none", ""
    if analyze.has_real_website(w):
        return "real", w
    if re.search(r"\.[a-z]{2,}", w.lower()):     # a link, but social/builder/directory
        return "social", w
    return "none", ""


def social_links(website, status):
    """Build the social_links jsonb: {platform: url} for a social/builder link."""
    if status != "social" or not website:
        return {}
    host = website.lower()
    for needle, platform in SOCIAL_PLATFORMS:
        if needle in host:
            return {platform: website}
    return {"other": website}        # a link, social-ish, but unrecognised host


def extract_coords(maps_url):
    """(lat, lng) parsed from the @lat,lng in a Maps place URL, or (None, None)."""
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", maps_url or "")
    if m:
        return m.group(1), m.group(2)
    return None, None


def extract_postal(address):
    m = re.search(r"\b(\d{6})\b", address or "")
    return m.group(1) if m else ""


def clean_area(address, postal):
    a = (address or "").strip()
    if postal:
        a = a.replace(postal, "")
    a = re.sub(r",?\s*India\s*$", "", a, flags=re.I)
    return re.sub(r"[\s,]+$", "", a).strip(" ,")


def canonical_maps_link(name, place_id, fallback):
    if place_id.startswith("ChIJ"):
        q = urllib.parse.quote(name)
        return (f"https://www.google.com/maps/search/?api=1&query={q}"
                f"&query_place_id={place_id}")
    return fallback or ""


def score_0_100(rating, reviews):
    """0-100 score matching the existing leads.score scale (60% rating, 40% volume)."""
    try:
        r = float(rating or 0)
        n = int(float(reviews or 0))
    except (ValueError, TypeError):
        return None
    return round(min(100.0, (r / 5.0) * 60.0
                     + min(40.0, (math.log10(n + 1) / math.log10(1000)) * 40.0)))


# ---- SQL literal helpers ----------------------------------------------------
def _s(v):                       # text / enum literal
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _enum(v, typ):               # text cast to a named enum type
    return "NULL" if not v else f"'{v}'::{typ}"


def _int(v):
    if v is None or str(v).strip() == "":
        return "NULL"
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return "NULL"


def _rating(v):                  # numeric(2,1) — one decimal, 0-9.9
    if v is None or str(v).strip() == "":
        return "NULL"
    try:
        return f"{round(float(v), 1)}"
    except (ValueError, TypeError):
        return "NULL"


def _num(v):                     # plain numeric (lat/lng) or NULL
    if v is None or str(v).strip() == "":
        return "NULL"
    try:
        return str(float(v))
    except (ValueError, TypeError):
        return "NULL"


def _jsonb(obj):                 # python obj -> '...'::jsonb literal
    return "'" + json.dumps(obj, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def _jsonb_raw(s):               # a JSON string already (from the CSV) -> jsonb
    s = (s or "").strip()
    if not s:
        return "'{}'::jsonb"
    return "'" + s.replace("'", "''") + "'::jsonb"


def _text_array(items):          # python list -> ARRAY[...]::text[] (or empty)
    if not items:
        return "ARRAY[]::text[]"
    inner = ",".join("'" + str(i).replace("'", "''") + "'" for i in items)
    return f"ARRAY[{inner}]::text[]"


def _tags_from_csv(v):
    """rank_tags arrives from the CSV as a ';'-joined string."""
    if isinstance(v, list):
        return v
    return [t for t in (v or "").split(";") if t]


def row_to_values(row, dataset):
    name = row.get("name", "")
    phone = row.get("phone", "")
    address = row.get("address", "")
    postal = extract_postal(address)
    area = row.get("area") or clean_area(address, postal)
    status, url = classify_website(row.get("website", ""))
    socials = social_links(row.get("website", ""), status)
    lat, lng = extract_coords(row.get("maps_url", ""))
    pid = place_id_for(row)
    maps_link = canonical_maps_link(name, pid, row.get("maps_url", ""))

    # ranking fields come straight from analyze (single source of truth in
    # ranking.py). Fall back to the 0-100 score if an older csv lacks them.
    sc = row.get("score")
    if sc is None or str(sc).strip() == "":
        sc = score_0_100(row.get("rating"), row.get("reviews"))

    vals = [
        _s(name),
        _s(phone),
        _s(area),
        _s(postal),
        _s(address),                              # full_address
        _num(lat),                                # latitude
        _num(lng),                                # longitude
        _rating(row.get("rating")),
        _int(row.get("reviews")),
        _s(maps_link),
        _s(url),                                  # website_url
        _enum(status, "website_status"),
        _jsonb(socials),                          # social_links
        _s(row.get("category", "")),
        _enum(LEAD_SOURCE, "lead_source"),
        _s(dataset),
        _s(pid),
        _int(sc) if sc is not None else "NULL",
        _s(row.get("tier")),                      # tier (text + check)
        _text_array(_tags_from_csv(row.get("rank_tags"))),   # rank_tags text[]
        _jsonb_raw(row.get("score_breakdown")),   # score_breakdown jsonb
        _jsonb(dict(row)),                        # raw — full scraped row
    ]
    return "(" + ",".join(vals) + ")"


def generate(rows, dataset, only_leads=True):
    """Return (sql_text, n_rows). `dataset` lands in source_dataset (provenance)."""
    if only_leads:
        rows = [r for r in rows
                if r.get("website_status") == "NO-SITE"
                or not analyze.has_real_website(r.get("website", ""))]

    seen_pid, seen_phone, values = set(), set(), []
    for r in rows:
        pid = place_id_for(r)
        ph = norm_phone(r.get("phone", ""))
        if pid in seen_pid or (ph and ph in seen_phone):   # intra-batch dedup
            continue
        seen_pid.add(pid)
        if ph:
            seen_phone.add(ph)
        values.append(row_to_values(r, dataset))

    if not values:
        return "-- no new rows to insert\n", 0

    cols = ",".join(COLUMNS)
    header = (
        f"-- {dataset}: {len(values)} candidate leads → KunthiveOS `leads`\n"
        f"-- INSERT-only. Existing rows are never touched.\n"
        f"-- Skips anything already present by source_place_id OR phone, and\n"
        f"-- the UNIQUE(source_place_id) + ON CONFLICT is the final backstop.\n"
        f"-- Run in the Supabase SQL editor (service role; bypasses RLS). Safe to re-run.\n\n")
    # Cast the numeric columns explicitly: an all-NULL column in a VALUES list is
    # typed `text` by Postgres, which then won't insert into a numeric/int column.
    casts = {"latitude": "::numeric", "longitude": "::numeric", "rating": "::numeric",
             "reviews_count": "::int", "score": "::int"}
    select_list = ",".join("v." + c + casts.get(c, "") for c in COLUMNS)
    body = (
        f"INSERT INTO {TABLE} ({cols})\n"
        f"SELECT {select_list}\n"
        f"FROM (VALUES\n  " + ",\n  ".join(values) + f"\n) AS v({cols})\n"
        f"WHERE NOT EXISTS (\n"
        f"  SELECT 1 FROM {TABLE} l\n"
        f"  WHERE l.source_place_id = v.source_place_id\n"
        f"     OR ( nullif(regexp_replace(coalesce(l.phone,''),'\\D','','g'),'')\n"
        f"          = nullif(regexp_replace(coalesce(v.phone,''),'\\D','','g'),'') )\n"
        f")\n"
        f"ON CONFLICT (source_place_id) DO NOTHING;\n")
    return header + body, len(values)


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description="CSV -> KunthiveOS leads INSERT SQL.")
    ap.add_argument("csv", help="a *-LEADS.csv (or *-ALL.csv) from analyze")
    ap.add_argument("--dataset", help="source_dataset value (default: argora/<stem>)")
    ap.add_argument("--out", help="write SQL here (default: stdout)")
    ap.add_argument("--include-has-site", action="store_true",
                    help="also include businesses that HAVE a real website")
    args = ap.parse_args(argv)

    stem = os.path.basename(args.csv).replace("-LEADS.csv", "").replace(".csv", "")
    dataset = args.dataset or f"argora/{stem}"
    rows = load_csv(args.csv)
    sql, n = generate(rows, dataset, only_leads=not args.include_has_site)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            f.write(sql)
        print(f"Wrote {n} INSERT rows → {args.out}")
    else:
        print(sql)


if __name__ == "__main__":
    main()
