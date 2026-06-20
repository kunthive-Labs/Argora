#!/usr/bin/env python3
"""
app.py — launch the maps-lead-finder web app and open it in your browser.

    python app.py            # -> http://127.0.0.1:8000 opens automatically
    python app.py --port 9000 --no-open

This is the ONE command. The UI lets you pick categories + location, hit Go,
watch the scrape live, browse the extracted CSVs, and generate Supabase SQL.
"""
import argparse
import threading
import webbrowser

import uvicorn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"\n  maps-lead-finder → {url}\n  (Ctrl-C to stop)\n")
    uvicorn.run("webapp.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
