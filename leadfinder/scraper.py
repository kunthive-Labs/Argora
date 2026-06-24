"""
scraper.py — Google Maps scraper (Playwright baseline).

Scrapes one search term in one area, scrolling the results rail and pulling
name / rating / reviews / category / address / phone / website for each place.
Writes a raw JSON list that analyze.py then turns into lead CSVs.

This is the FRAGILE layer. Google rotates its DOM every few months; when
extraction starts returning blanks, the selectors in SEL below are the only
thing you update.

Constraints baked in (true of every Maps scraper, paid or free):
  - Google caps a search at ~120 results. Scrape area-by-area, not one big query.
  - Runs HEADED and gently (human-like pauses) to avoid IP throttling.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python -m leadfinder.scraper "driving school" "Jayanagar, Bengaluru" \\
        --out data/raw/driving-jayanagar.json --max 120
"""
import argparse
import json
import re
import time

# Selectors are isolated here on purpose — the one place to fix when Google
# changes its markup. (Confirmed shapes as of early 2026; verify if blanks.)
SEL = {
    "results_feed": 'div[role="feed"]',
    "result_card": 'div[role="feed"] > div > div[jsaction]',
    "result_link": "a.hfpxzc",                 # each place card's anchor
    "detail_name": "h1",
    "detail_rating": 'div.F7nice span[aria-hidden="true"]',
    "detail_reviews": 'div.F7nice span[aria-label]',
    "detail_category": "button[jsaction*='category']",
    "btn_website": 'a[data-item-id="authority"]',
    "btn_phone": 'button[data-item-id^="phone"]',
    "detail_address": 'button[data-item-id="address"]',
}


def _txt(page, selector):
    el = page.query_selector(selector)
    return el.inner_text().strip() if el else ""


def _attr(page, selector, attr):
    el = page.query_selector(selector)
    return el.get_attribute(attr) if el else ""


class ScrapeError(Exception):
    """Raised when scraping is interrupted but partial results are available."""
    def __init__(self, message, results):
        super().__init__(message)
        self.results = results


def scrape(search, location, max_results=120, headless=False, pause=1.2,
           log=print, should_stop=None):
    """Scrape places. `log` receives progress strings (the web UI passes a
    callback that streams them live). `should_stop` is an optional callable
    returning True to abort gracefully mid-run."""
    from playwright.sync_api import sync_playwright

    query = f"{search} in {location}"
    results = []
    stop = should_stop or (lambda: False)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            page.goto(f"https://www.google.com/maps/search/{query.replace(' ', '+')}",
                      timeout=60000)
            page.wait_for_timeout(3000)

            # scroll the results rail until it stops growing or we hit max
            try:
                page.wait_for_selector(SEL["results_feed"], timeout=15000)
            except Exception as e:
                log("  ! results feed never appeared — selector may be stale "
                    "(update SEL['results_feed'])")
                browser.close()
                raise ScrapeError("Results feed never appeared", results) from e

            seen_links = []
            stagnant = 0
            while len(seen_links) < max_results and stagnant < 5 and not stop():
                links = page.query_selector_all(SEL["result_link"])
                urls = [l.get_attribute("href") for l in links if l.get_attribute("href")]
                new = [u for u in urls if u not in seen_links]
                if new:
                    seen_links.extend(new)
                    stagnant = 0
                else:
                    stagnant += 1
                page.eval_on_selector(SEL["results_feed"],
                                      "el => el.scrollBy(0, el.scrollHeight)")
                page.wait_for_timeout(int(pause * 1000))

            seen_links = seen_links[:max_results]
            log(f"  found {len(seen_links)} place cards; opening each…")

            for i, url in enumerate(seen_links, 1):
                if stop():
                    log("  · stop requested — finishing early")
                    break
                try:
                    page.goto(url, timeout=30000)
                    page.wait_for_selector(SEL["detail_name"], timeout=10000)
                    page.wait_for_timeout(int(pause * 600))
                    rec = {
                        "name": _txt(page, SEL["detail_name"]),
                        "rating": _txt(page, SEL["detail_rating"]),
                        "reviews": re.sub(r"[^\d]", "",
                                          _attr(page, SEL["detail_reviews"], "aria-label") or ""),
                        "category": _txt(page, SEL["detail_category"]),
                        "website": _attr(page, SEL["btn_website"], "href") or "",
                        "phone": (_attr(page, SEL["btn_phone"], "aria-label") or "")
                                 .replace("Phone: ", ""),
                        "address": (_attr(page, SEL["detail_address"], "aria-label") or "")
                                   .replace("Address: ", ""),
                        "mapsUrl": url,
                    }
                    results.append(rec)
                    log(f"    [{i}/{len(seen_links)}] {rec['name']or '(no name)'}"
                        f"{'  · NO-SITE' if not rec['website'] else ''}")
                except Exception as e:
                    log(f"    ! skipped card {i}: {e}")
                    if not browser.is_connected():
                        log("    ! browser disconnected — aborting scrape loop")
                        raise ScrapeError(f"Browser disconnected: {e}", results) from e
                    continue

            browser.close()
    except ScrapeError:
        raise
    except Exception as e:
        raise ScrapeError(str(e), results) from e

    return results



def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrape Google Maps places to JSON.")
    ap.add_argument("search", help='e.g. "driving school"')
    ap.add_argument("location", help='e.g. "Jayanagar, Bengaluru"')
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--max", type=int, default=120, help="cap (Google maxes ~120)")
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window (higher throttle risk)")
    args = ap.parse_args(argv)

    print(f"Scraping '{args.search}' in '{args.location}' (max {args.max})…")
    records = scrape(args.search, args.location, args.max, args.headless)

    with open(args.out, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(records)} places → {args.out}")
    print(f"Next: python -m leadfinder.analyze {args.out} --out data/leads/<stem> --sector <name>")


if __name__ == "__main__":
    main()
