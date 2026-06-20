"""
analyze.py — the lead-intelligence layer.

Takes raw scraped places (JSON list of dicts from ANY engine — this scraper,
omkarcloud, gosom, Apify) and produces:

  <stem>-LEADS.csv       no-website businesses with a phone, ranked
  <stem>-COMPETITORS.csv same-category businesses that DO have a website, ranked
                         (your pitch ammo: "your neighbour has a site + N reviews")
  <stem>-ALL.csv         everything, with a website_status column

It auto-normalizes column names, so it doesn't care which scraper produced the
input. It dedupes, treats empty / "none" / social-only links as "no website",
and ranks each lead with the 5-dimension algorithm in `ranking.py`
(score 0–100, tier, tags, breakdown) — see
KunthiveOS/docs/handover-lead-ranking-algorithm.md.

Usage:
    python -m leadfinder.analyze raw/driving-jayanagar.json \\
        --out data/leads/driving-jayanagar \\
        --sector driving-school          # applies sector excludes + review floor
    # or merge several area pulls into one deduped list:
    python -m leadfinder.analyze raw/re-*.json --out data/leads/real-estate --min-reviews 5
"""
import argparse
import csv
import glob
import json
import re
import sys

from leadfinder import ranking

# ---- column normalization ---------------------------------------------------
# Map the many names different scrapers use onto our canonical fields.
FIELD_ALIASES = {
    "name":        ["name", "title", "placeName", "business_name"],
    "phone":       ["phone", "phoneNumber", "phone_number", "telephone", "PHONE"],
    "website":     ["website", "url", "site", "web", "WEBSITE", "domain"],
    "address":     ["address", "fullAddress", "formatted_address", "ADDRESS"],
    "category":    ["category", "categoryName", "mainCategory", "MAIN_CATEGORY",
                    "type", "primaryCategory"],
    "rating":      ["rating", "stars", "totalScore", "RATING", "averageRating"],
    "reviews":     ["reviews", "reviewsCount", "reviewCount", "user_ratings_total",
                    "REVIEWS", "numberOfReviews"],
    "maps_url":    ["mapsUrl", "googleMapsUrl", "link", "url_maps", "placeUrl"],
}

# A "website" that is really just a social / builder page = still a lead.
SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "instagram.com", "instagr.am", "twitter.com",
    "x.com", "linkedin.com", "youtube.com", "wa.me", "whatsapp.com",
    "linktr.ee", "justdial.com", "indiamart.com", "sulekha.com",
    "google.com", "g.page", "business.site",  # google business builder pages
)


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return ""


def normalize(raw):
    """Map a raw place dict to canonical fields."""
    rec = {canon: _first(raw, aliases) for canon, aliases in FIELD_ALIASES.items()}
    # coerce numerics
    rec["reviews"] = _to_int(rec["reviews"])
    rec["rating"] = _to_float(rec["rating"])
    rec["phone"] = str(rec["phone"]).strip()
    rec["website"] = str(rec["website"]).strip()
    rec["name"] = str(rec["name"]).strip()
    return rec


def _to_int(v):
    try:
        return int(re.sub(r"[^\d]", "", str(v)) or 0)
    except (ValueError, TypeError):
        return 0


def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def has_real_website(website):
    """True only if there's a genuine business site (not blank/none/social).
    Delegates to ranking.web_status so the social/builder list is single-source."""
    return ranking.web_status(website) == "real"


def dedupe(records):
    """Drop duplicate places (same scrape merged from overlapping areas)."""
    seen = set()
    out = []
    for r in records:
        key = (r["name"].lower(), r["phone"]) if r["phone"] else (r["name"].lower(),)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _postal(address):
    m = re.search(r"\b(\d{6})\b", address or "")
    return m.group(1) if m else ""


def matches_sector(r, exclude):
    """False if the record's category/name hits an exclude keyword."""
    hay = f"{r['category']} {r['name']}".lower()
    return not any(kw.lower() in hay for kw in exclude)


def analyze(records, exclude=None, min_reviews=0):
    exclude = exclude or []
    records = [normalize(r) for r in records]
    records = dedupe(records)
    records = [r for r in records if matches_sector(r, exclude)]
    records = [r for r in records if r["reviews"] >= min_reviews]

    # phones appearing on more than one record (Step 4: 'duplicate' tag)
    phone_counts = {}
    for r in records:
        if r["phone"]:
            phone_counts[r["phone"]] = phone_counts.get(r["phone"], 0) + 1

    for r in records:
        r["has_website"] = has_real_website(r["website"])
        r["website_status"] = "has-site" if r["has_website"] else "NO-SITE"
        res = ranking.rank(
            name=r["name"], phone=r["phone"], website=r["website"],
            category=r["category"], postal=_postal(r.get("address", "")),
            rating=r["rating"] or None, reviews=r["reviews"],
            is_duplicate=phone_counts.get(r["phone"], 0) > 1)
        r["score"] = res["score"]
        r["tier"] = res["tier"]
        r["rank_tags"] = res["tags"]
        r["score_breakdown"] = res["breakdown"]
        r["web_status"] = res["web_status"]
        r["disqualified"] = res["disqualified"]

    # leads = no real website, reachable, and not hard-disqualified — ranked
    leads = [r for r in records
             if not r["has_website"] and r["phone"] and not r["disqualified"]]
    leads.sort(key=ranking.sort_key, reverse=True)
    competitors = [r for r in records if r["has_website"]]
    competitors.sort(key=ranking.sort_key, reverse=True)
    records.sort(key=ranking.sort_key, reverse=True)
    return records, leads, competitors


# ---- io ---------------------------------------------------------------------
COLUMNS = ["name", "phone", "category", "area", "rating", "reviews",
           "website", "website_status", "score", "tier", "rank_tags",
           "score_breakdown", "address", "maps_url"]


def _csv_value(v):
    """Serialise list/dict cells (rank_tags, score_breakdown) for the CSV."""
    if isinstance(v, list):
        return ";".join(v)
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"))
    return v


def load(paths):
    records = []
    for pattern in paths:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            with open(path) as f:
                data = json.load(f)
            records.extend(data if isinstance(data, list) else data.get("items", []))
    return records


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r.setdefault("area", "")
            w.writerow({c: _csv_value(r.get(c, "")) for c in COLUMNS})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build LEADS / COMPETITORS / ALL CSVs.")
    ap.add_argument("inputs", nargs="+", help="raw JSON file(s) or glob(s)")
    ap.add_argument("--out", required=True, help="output stem, e.g. data/leads/driving")
    ap.add_argument("--sector", help="sector preset name (applies excludes + review floor)")
    ap.add_argument("--min-reviews", type=int, help="override review floor")
    ap.add_argument("--exclude", nargs="*", default=[], help="extra exclude keywords")
    args = ap.parse_args(argv)

    exclude = list(args.exclude)
    min_reviews = args.min_reviews if args.min_reviews is not None else 0
    if args.sector:
        from leadfinder import sectors
        s = sectors.get(args.sector)
        exclude += s["exclude"]
        if args.min_reviews is None:
            min_reviews = s["min_reviews"]

    raw = load(args.inputs)
    if not raw:
        sys.exit("No records loaded — check the input path.")

    allrec, leads, competitors = analyze(raw, exclude, min_reviews)

    write_csv(f"{args.out}-ALL.csv", allrec)
    write_csv(f"{args.out}-LEADS.csv", leads)
    write_csv(f"{args.out}-COMPETITORS.csv", competitors)

    print(f"  scraped/kept : {len(allrec)}")
    print(f"  LEADS (no site + phone) : {len(leads)}  -> {args.out}-LEADS.csv")
    print(f"  COMPETITORS (has site)  : {len(competitors)}  -> {args.out}-COMPETITORS.csv")
    if leads:
        top = leads[0]
        print(f"  hottest lead : {top['name']} — {top['reviews']} reviews, "
              f"{top['rating']}★, {top['phone']}")


if __name__ == "__main__":
    main()
