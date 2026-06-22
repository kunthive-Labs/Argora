"""
extractor.py — pull text from an exact URL; fall back to full-page screenshots.

Given ONE exact URL, this:
  1. Opens the page (Playwright, same engine as scraper.py).
  2. Reads the page's selectable text (document.body.innerText).
  3. If that text is meaningful, saves it to  data/extracts/<page>/text.txt
  4. If the page has little/no *selectable* text (image-only pages, canvas
     renders, scanned docs, etc.), it creates a folder named after the page and
     saves a COMPLETE full-page screenshot there instead — so you still capture
     the content even when it can't be copied as text.

`mode` controls the behaviour:
  "auto"        — text if selectable, else screenshot (the default)
  "text"        — only attempt text extraction
  "screenshot"  — always capture the full page as an image
  "both"        — save text (if any) AND a full-page screenshot

This reuses scraper.py's headed/gentle posture. It opens a single page — no
scrolling rail, no Google — so it is far lighter than a Maps scrape.

Usage (CLI):
    python -m leadfinder.extractor "https://example.com/page" \\
        --out data/extracts --mode auto
"""
import argparse
import os
import re
from urllib.parse import urlparse

# A page with fewer than this many non-whitespace characters of selectable text
# is treated as "no real text" → we screenshot it instead.
MIN_TEXT_CHARS = 40


def _slug(s, fallback="page"):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return (s or fallback)[:80]


def _folder_name(url, title):
    """Name the folder after the page: its <title>, else host + path."""
    if title and title.strip():
        return _slug(title)
    p = urlparse(url)
    base = (p.netloc + p.path).rstrip("/")
    return _slug(base, fallback="page")


def _scroll_through(page, pause_ms=400, max_steps=40):
    """Scroll top→bottom in steps so lazy-loaded images render before we shoot
    a full-page screenshot, then return to the top."""
    try:
        height = page.evaluate("document.body.scrollHeight") or 0
        step = max(page.evaluate("window.innerHeight") or 800, 400)
        y = 0
        steps = 0
        while y < height and steps < max_steps:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(pause_ms)
            y += step
            steps += 1
            height = page.evaluate("document.body.scrollHeight") or height
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(pause_ms)
    except Exception:
        pass  # best-effort; a failed scroll never blocks the capture


def extract(url, out_root, mode="auto", headless=True, pause=1.0,
            log=print, should_stop=None):
    """Extract one URL. Returns a result dict describing what was saved:
        {url, title, folder, mode, text_file, text_chars, screenshots:[...]}
    `log` receives progress strings (the web UI streams them live).
    `should_stop` is an optional callable returning True to abort.
    """
    from playwright.sync_api import sync_playwright

    stop = should_stop or (lambda: False)
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    result = {"url": url, "title": "", "folder": "", "mode": mode,
              "text_file": "", "text_chars": 0, "screenshots": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            log(f"▶ opening {url}")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(int(pause * 1500))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # some pages never go idle; carry on

            if stop():
                log("  · stop requested before capture")
                browser.close()
                return result

            title = (page.title() or "").strip()
            result["title"] = title
            folder = _folder_name(url, title)
            folder_path = os.path.join(out_root, folder)
            os.makedirs(folder_path, exist_ok=True)
            result["folder"] = folder
            log(f"  folder → {folder}")

            # ---- read selectable text --------------------------------------
            text = ""
            if mode in ("auto", "text", "both"):
                try:
                    text = page.evaluate(
                        "document.body ? document.body.innerText : ''") or ""
                except Exception:
                    text = ""
                cleaned = text.strip()
                n = len(re.sub(r"\s", "", cleaned))
                log(f"  selectable text: {n} chars")

                want_text = (mode in ("text", "both")) or \
                            (mode == "auto" and n >= MIN_TEXT_CHARS)
                if want_text and cleaned:
                    # save the page URL + title as a small header for context
                    header = f"# {title or url}\n# {url}\n\n"
                    txt_path = os.path.join(folder_path, "text.txt")
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(header + cleaned)
                    result["text_file"] = os.path.join(folder, "text.txt")
                    result["text_chars"] = len(cleaned)
                    log(f"  ✓ text saved → {result['text_file']}")

            # ---- decide whether to screenshot ------------------------------
            n_clean = len(re.sub(r"\s", "", text.strip()))
            need_shot = (
                mode == "screenshot" or mode == "both" or
                (mode == "auto" and n_clean < MIN_TEXT_CHARS))

            if need_shot:
                if mode == "auto":
                    log("  ! little/no selectable text — capturing full page")
                _scroll_through(page)
                shot_path = os.path.join(folder_path, "screenshot-full.png")
                try:
                    page.screenshot(path=shot_path, full_page=True)
                    result["screenshots"].append(
                        os.path.join(folder, "screenshot-full.png"))
                    log(f"  ✓ full-page screenshot → {folder}/screenshot-full.png")
                except Exception as e:
                    # very tall pages can exceed the image size limit; fall back
                    # to a viewport-by-viewport capture so we still get it all.
                    log(f"  ! full-page shot failed ({e}); tiling viewports")
                    result["screenshots"] += _tile_screenshots(
                        page, folder_path, folder, log)

            if not result["text_file"] and not result["screenshots"]:
                log("  ! nothing captured (empty page?)")
        finally:
            browser.close()
    return result


def _tile_screenshots(page, folder_path, folder, log, max_tiles=30):
    """Fallback for pages too tall for one PNG: capture the viewport, scroll a
    screenful, repeat. Returns the list of relative screenshot paths."""
    shots = []
    try:
        vh = page.evaluate("window.innerHeight") or 900
        total = page.evaluate("document.body.scrollHeight") or vh
        y, i = 0, 0
        while y < total and i < max_tiles:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(350)
            name = f"screenshot-{i:02d}.png"
            page.screenshot(path=os.path.join(folder_path, name))
            shots.append(os.path.join(folder, name))
            y += vh
            i += 1
            total = page.evaluate("document.body.scrollHeight") or total
        log(f"  ✓ captured {len(shots)} viewport tiles")
    except Exception as e:
        log(f"  ! tiling failed: {e}")
    return shots


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract text or screenshots from a URL.")
    ap.add_argument("url", help="exact page URL, e.g. https://example.com/about")
    ap.add_argument("--out", default="data/extracts", help="root output folder")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "text", "screenshot", "both"])
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    res = extract(args.url, args.out, mode=args.mode, headless=args.headless)
    print("\n--- result ---")
    print(f"  folder      : {args.out}/{res['folder']}")
    print(f"  title       : {res['title'] or '(none)'}")
    if res["text_file"]:
        print(f"  text        : {res['text_file']} ({res['text_chars']} chars)")
    for s in res["screenshots"]:
        print(f"  screenshot  : {s}")
    if not res["text_file"] and not res["screenshots"]:
        print("  (nothing captured)")


if __name__ == "__main__":
    main()
