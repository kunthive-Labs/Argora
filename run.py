#!/usr/bin/env python3
"""
run.py — one command: scrape a sector in an area, then build the lead CSVs.

    python run.py driving-school "Jayanagar, Bengaluru"
    python run.py real-estate "Whitefield, Bengaluru" --max 120

Output lands in data/raw/<stem>.json and data/leads/<stem>-LEADS.csv (+COMPETITORS/ALL).
For merging several areas into one deduped list, scrape each, then call
`python -m leadfinder.analyze data/raw/re-*.json --out data/leads/real-estate --sector real-estate`.
"""
import argparse
import re

from leadfinder import analyze, scraper, sectors


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sector", help=f"one of: {', '.join(sorted(sectors.SECTORS))}")
    ap.add_argument("location", help='e.g. "Jayanagar, Bengaluru"')
    ap.add_argument("--max", type=int, default=120)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    s = sectors.get(args.sector)
    stem = f"{args.sector}-{slug(args.location)}"
    raw_path = f"data/raw/{stem}.json"
    out_stem = f"data/leads/{stem}"

    print(f"== {args.sector} @ {args.location} ==")
    records = []
    scrape_err = None
    try:
        records = scraper.scrape(s["query"], args.location, args.max, args.headless)
    except scraper.ScrapeError as e:
        records = e.results
        scrape_err = e
        print(f"  ! scraping failed mid-way: {e}")

    import json
    with open(raw_path, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"raw → {raw_path}")

    analyze.main([raw_path, "--out", out_stem, "--sector", args.sector])

    if scrape_err:
        raise scrape_err


if __name__ == "__main__":
    main()
