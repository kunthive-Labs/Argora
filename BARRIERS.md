# BARRIERS — what NOT to do (so this keeps working and you don't get banned)

This tool scrapes public Google Maps listings. That sits in Google's **ToS grey
zone** — fine for modest, legitimate B2B prospecting, risky if you push it.
Read this before you scale.

## 1. Throttling / soft-bans (the thing that actually breaks it)

Google watches for automation. Cross the line and you get a CAPTCHA wall, a
"our systems have detected unusual traffic" page, or a temporary IP block.

**Don't:**
- ❌ Run **headless** at volume — it's the easiest bot tell. Run **headed** (the default).
- ❌ Scrape the **same area + category repeatedly** in a short window.
- ❌ Remove the human-like **pauses** in `scraper.py` to go "faster".
- ❌ Run **multiple jobs in parallel** (the app enforces one at a time — keep it that way).
- ❌ Blast dozens of areas back-to-back. Space runs out over hours, not minutes.

**Do:** a few areas per session, headed, normal pace. If you hit a CAPTCHA,
**stop for the day** — don't fight it.

## 2. Don't build anti-bot evasion

The moment you add **CAPTCHA-solving, proxy/IP rotation, residential proxies,
randomised fingerprints, or login automation**, you cross from "grey-area
prospecting" into deliberate ToS circumvention. That's what gets accounts
terminated and is legally far riskier. This project deliberately has none of it.
Keep it that way.

## 3. Never log into Google in the automated browser

Scrape **logged out**. Driving Maps while signed in ties the automation to your
Google account — that's how you get the *account* (not just the IP) flagged.

## 4. Don't commit scraped data

`data/raw/`, `data/leads/`, `data/sql/` are **git-ignored** on purpose. They
contain business names + phone numbers (PII). Pushing a public repo full of
scraped phone lists is a privacy problem and a ToS problem. The `.gitkeep` files
keep the folders; the contents stay local.

## 5. Outreach hygiene (India / TRAI)

These are leads for **legitimate, low-volume, relevant** outreach — a business
that has no website, contacted about a website. Don't feed them into bulk
auto-dialers or spam blasts: India's TRAI/DND rules on unsolicited commercial
calls and messages are real, and spamming burns the brand you're trying to build.

## 6. Selector rot (not a ban — a maintenance fact)

Google rewrites its Maps DOM every few months. When the scrape starts returning
blank names/phones/websites, the cause is almost always the **`SEL` dict in
`leadfinder/scraper.py`** — that's the one place to update. Nothing else breaks.

## 7. The hard limits no tool can beat

- **~120 results per search.** Scrape neighbourhood-by-neighbourhood, not "Bengaluru".
- **No emails.** Maps exposes phone + website, not email. Don't expect it.
- **Place-id formats differ.** The free scraper records a feature-id hex;
  your older Apify imports used `ChIJ...`. Dedup is reliable *within* each
  source, not guaranteed *across* them.

## TL;DR

Small, headed, spaced-out runs. No evasion tooling. Logged out. Data stays local.
Outreach stays legit. Update `SEL` when extraction goes blank.
