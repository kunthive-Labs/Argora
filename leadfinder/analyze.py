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
and ranks by a transparent reviews * rating score.

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
    """True only if there's a genuine business site (not blank/none/social)."""
    w = website.strip().lower()
    if not w or w in ("none", "n/a", "na", "-", "null"):
        return False
    if not re.search(r"\.[a-z]{2,}", w):  # no domain-looking thing at all
        return False
    return not any(host in w for host in SOCIAL_HOSTS)


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


def score(r):
    """Transparent ranking: more reviews + higher rating = hotter lead.
    log-free, intentionally simple so it's explainable to yourself."""
    return r["reviews"] * (r["rating"] if r["rating"] else 3.0)


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

    for r in records:
        r["has_website"] = has_real_website(r["website"])
        r["website_status"] = "has-site" if r["has_website"] else "NO-SITE"
        r["score"] = round(score(r), 1)

    records.sort(key=lambda r: r["score"], reverse=True)

    leads = [r for r in records if not r["has_website"] and r["phone"]]
    competitors = [r for r in records if r["has_website"]]
    return records, leads, competitors


# ---- io ---------------------------------------------------------------------
COLUMNS = ["name", "phone", "category", "area", "rating", "reviews",
           "website", "website_status", "score", "address", "maps_url"]


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
            w.writerow(r)


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
