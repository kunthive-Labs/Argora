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


def _strip_label(s):
    """Drop a leading 'Label: ' prefix from an aria-label, locale-agnostic
    ('Phone: 080…', 'Telefon: +49…', 'Address: 12, MG Road')."""
    return re.sub(r"^[^:,\d+]{1,24}:\s*", "", (s or "").strip())


# ── CAPTCHA / throttle wall ───────────────────────────────────────────────────
# Google's rate-limit interstitial ("our systems have detected unusual traffic")
# lives under /sorry/. We DETECT it and stop — per BARRIERS.md we never solve,
# rotate, or otherwise evade; the only right response is to quit for the day.
CAPTCHA_URL_MARKER = "/sorry/"
CAPTCHA_TEXT_MARKERS = ("unusual traffic from your", "systems have detected unusual")
CAPTCHA_STOP_MSG = ("Google throttle/CAPTCHA wall detected — this IP has been "
                    "rate-limited. Stop scraping for the day (see BARRIERS.md §1); "
                    "don't retry now and don't fight it.")


def is_captcha_page(url, content=""):
    """True if the browser landed on Google's throttle/CAPTCHA interstitial.
    URL check first (cheap, exact); the text markers are deliberately narrow
    phrases from the interstitial so normal Maps content can't false-positive."""
    if CAPTCHA_URL_MARKER in (url or "").lower():
        return True
    c = (content or "").lower()
    return any(m in c for m in CAPTCHA_TEXT_MARKERS)


def _hit_captcha(page):
    """is_captcha_page against a live page. page.content() can itself throw
    mid-navigation — fall back to the URL-only check."""
    try:
        return is_captcha_page(page.url, page.content())
    except Exception:
        return is_captcha_page(page.url)


# ── field-yield report ────────────────────────────────────────────────────────
# After a scrape, how many results actually carried each field? A sudden drop
# means the matching SEL entry went stale (Google DOM change) — the failure
# mode is otherwise silent blanks. `website` is deliberately excluded: low
# website yield is the product working, not selector rot.
YIELD_FIELDS = {  # record field -> SEL key to blame in the warning
    "name": "detail_name", "rating": "detail_rating", "reviews": "detail_reviews",
    "category": "detail_category", "address": "detail_address", "phone": "btn_phone",
}
YIELD_WARN = {"name": 0.90, "rating": 0.60, "reviews": 0.60,
              "category": 0.60, "address": 0.60, "phone": 0.30}
MIN_YIELD_SAMPLE = 10   # below this, low yield is noise, not signal


def field_yield(results):
    """{field: fraction of results with a non-empty value}, {} when empty."""
    if not results:
        return {}
    n = len(results)
    return {f: sum(1 for r in results if str(r.get(f) or "").strip()) / n
            for f in YIELD_FIELDS}


def report_field_yield(results, log=print):
    """Log a one-line yield summary; warn loudly per field when a batch is big
    enough (>= MIN_YIELD_SAMPLE) and a field's yield is suspiciously low."""
    fy = field_yield(results)
    if not fy:
        return
    log("  field yield: " + ", ".join(f"{f} {fy[f]:.0%}" for f in YIELD_FIELDS))
    if len(results) < MIN_YIELD_SAMPLE:
        return
    for f, frac in fy.items():
        if frac < YIELD_WARN[f]:
            log(f"  !! LOW YIELD: only {frac:.0%} of {len(results)} places have "
                f"'{f}' — SEL['{YIELD_FIELDS[f]}'] may be stale (Google DOM "
                f"change). Update SEL in leadfinder/scraper.py.")


class ScrapeError(Exception):
    """Raised when scraping is interrupted but partial results are available.
    `fatal=True` means the whole run should stop (CAPTCHA wall, browser gone) —
    not just this search."""
    def __init__(self, message, results, fatal=False):
        super().__init__(message)
        self.results = results
        self.fatal = fatal


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
            if _hit_captcha(page):
                log(f"  ✗ {CAPTCHA_STOP_MSG}")
                browser.close()
                raise ScrapeError(CAPTCHA_STOP_MSG, results, fatal=True)

            # scroll the results rail until it stops growing or we hit max
            try:
                page.wait_for_selector(SEL["results_feed"], timeout=15000)
            except Exception as e:
                # a /sorry/ redirect is the most common cause of this timeout —
                # tell the user it's the throttle, not a stale selector
                if _hit_captcha(page):
                    log(f"  ✗ {CAPTCHA_STOP_MSG}")
                    browser.close()
                    raise ScrapeError(CAPTCHA_STOP_MSG, results, fatal=True) from e
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
                for attempt in (1, 2):      # one retry on a flaky detail page
                    try:
                        page.goto(url, timeout=30000)
                        if is_captcha_page(page.url):
                            log(f"  ✗ {CAPTCHA_STOP_MSG}")
                            browser.close()
                            raise ScrapeError(CAPTCHA_STOP_MSG, results, fatal=True)
                        page.wait_for_selector(SEL["detail_name"], timeout=10000)
                        page.wait_for_timeout(int(pause * 600))
                        rec = {
                            "name": _txt(page, SEL["detail_name"]),
                            "rating": _txt(page, SEL["detail_rating"]),
                            "reviews": re.sub(r"[^\d]", "",
                                              _attr(page, SEL["detail_reviews"], "aria-label") or ""),
                            "category": _txt(page, SEL["detail_category"]),
                            "website": _attr(page, SEL["btn_website"], "href") or "",
                            "phone": _strip_label(
                                _attr(page, SEL["btn_phone"], "aria-label")),
                            "address": _strip_label(
                                _attr(page, SEL["detail_address"], "aria-label")),
                            "mapsUrl": url,
                        }
                        results.append(rec)
                        log(f"    [{i}/{len(seen_links)}] {rec['name']or '(no name)'}"
                            f"{'  · NO-SITE' if not rec['website'] else ''}")
                        break
                    except ScrapeError:
                        raise
                    except Exception as e:
                        if not browser.is_connected():
                            log("    ! browser disconnected — aborting scrape loop")
                            raise ScrapeError(f"Browser disconnected: {e}",
                                              results, fatal=True) from e
                        # a CAPTCHA mid-run manifests as timeouts on every card —
                        # check the page before writing this off as one flaky card
                        if _hit_captcha(page):
                            log(f"  ✗ {CAPTCHA_STOP_MSG}")
                            browser.close()
                            raise ScrapeError(CAPTCHA_STOP_MSG, results,
                                              fatal=True) from e
                        if attempt == 1:
                            log(f"    ! card {i} failed ({e}) — retrying once")
                            page.wait_for_timeout(int(pause * 1000))
                        else:
                            log(f"    ! skipped card {i}: {e}")

            browser.close()
    except ScrapeError:
        raise
    except Exception as e:
        raise ScrapeError(str(e), results) from e

    report_field_yield(results, log)
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

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(records)} places → {args.out}")
    print(f"Next: python -m leadfinder.analyze {args.out} --out data/leads/<stem> --sector <name>")


if __name__ == "__main__":
    main()
