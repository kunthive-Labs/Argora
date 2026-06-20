"""
ranking.py — the lead-ranking algorithm (single source of truth, Python side).

Spec: KunthiveOS/docs/handover-lead-ranking-algorithm.md. The TypeScript twin
lives at KunthiveOS/lib/scoring.ts — keep the two in sync with that doc.

Ranks leads by pitch-conversion probability (not raw business size) on a 0–100
scale across five additive dimensions, applies hard disqualifications and
special-scenario bonuses/tags, then assigns a tier.

Main entry point: rank(...) -> {score, tier, tags, breakdown, disqualified, web_status}
"""
import re

# ── Step 1: web-presence classification ──────────────────────────────────────
# A "website" that is really only a social / site-builder page is still a lead.
SOCIAL_BUILDER = (
    "facebook.com", "instagram.com", "youtube.com", "youtu.be",
    "wixsite.com", "wix.com", "site123", "codedesign.app", "starzpages",
    "myshopmatic", "sites.google.com", "linktr.ee",
    # extras consistent with the scraper's own list
    "fb.com", "instagr.am", "twitter.com", "x.com", "linkedin.com",
    "wa.me", "whatsapp.com", "justdial.com", "indiamart.com", "sulekha.com",
    "g.page", "business.site",
)

PREMIUM_PINS = {
    "560001", "560008", "560034", "560038", "560041",
    "560066", "560068", "560076", "560102", "560103",
}


def web_status(website):
    """'none' | 'social' | 'real'. Real = a genuine business site (not a lead)."""
    w = (website or "").strip().lower()
    if not w or w in ("none", "n/a", "na", "-", "null"):
        return "none"
    if any(host in w for host in SOCIAL_BUILDER):
        return "social"
    if not re.search(r"\.[a-z]{2,}", w):     # no domain-looking thing at all
        return "none"
    return "real"


# ── Step 2: the five scoring dimensions ──────────────────────────────────────
def _web_gap(ws):                       # A, max 40
    return {"none": 40, "social": 25}.get(ws, 0)


def _review_volume(reviews):            # B, max 25
    n = reviews or 0
    if n <= 0:
        return 0
    if n <= 10:
        return 5
    if n <= 50:
        return 10
    if n <= 150:
        return 16
    if n <= 400:
        return 21
    return 25


def _rating_trust(rating):              # C, max 15
    if rating is None or rating == 0:
        return 6                        # neutral — new, don't penalise
    if rating < 3.0:
        return 0
    if rating < 3.5:
        return 4
    if rating < 4.0:
        return 8
    if rating < 4.5:
        return 12
    return 15


def _reachability(phone):               # D, max 10
    return 10 if (phone or "").strip() else 0


def _category_urgency(category):        # E, max 10
    c = (category or "").lower()
    if any(k in c for k in ("real estate", "property", "realty", "realtor", "broker")):
        return 10
    if any(k in c for k in ("driving school", "motor driving", "driving centre",
                            "driving center")):
        return 8
    if any(k in c for k in ("school", "college", "university", "institute", "academy")):
        return 8
    if any(k in c for k in ("clinic", "hospital", "doctor", "dentist", "medical")):
        return 7
    if any(k in c for k in ("restaurant", "cafe", "hotel", "bakery", "catering")):
        return 6
    if any(k in c for k in ("construction", "contractor", "builder", "interior")):
        return 5
    return 3


# ── Step 3: hard disqualification ────────────────────────────────────────────
def is_disqualified(name, phone, rating, reviews):
    n = reviews or 0
    p = (phone or "").strip()
    r = rating
    title = (name or "").lower()
    if not p and n < 5:                                   # ghost listing
        return True
    if r is not None and r < 2.5 and n >= 20:             # unhappy customers
        return True
    if "closed" in title:                                  # dead business
        return True
    if n == 0 and (r is None or r == 0) and not p:         # no signal at all
        return True
    return False


# ── Step 5: tier ─────────────────────────────────────────────────────────────
def tier_for(score):
    if score >= 75:
        return "hot"
    if score >= 55:
        return "warm"
    if score >= 35:
        return "cool"
    return "cold"


# ── main ─────────────────────────────────────────────────────────────────────
def rank(name="", phone="", website="", category="", postal="",
         rating=None, reviews=0, is_duplicate=False):
    """Full ranking for one lead. `rating` may be None (treated as 'new')."""
    ws = web_status(website)
    reviews = reviews or 0

    disq = is_disqualified(name, phone, rating, reviews)

    A = _web_gap(ws)
    B = _review_volume(reviews)
    C = _rating_trust(rating)
    D = _reachability(phone)
    E = _category_urgency(category)
    base = A + B + C + D + E

    # Step 4: special scenarios
    tags, bonus = [], 0
    if ws == "social" and reviews > 100:
        tags.append("upgrade_pitch"); bonus += 5
    if ws == "none" and reviews > 300:
        tags.append("iconic_local"); bonus += 5
    premium = bool(postal) and str(postal).strip() in PREMIUM_PINS
    if premium:
        tags.append("premium_zone"); bonus += 3
    if not (phone or "").strip() and reviews > 200:
        tags.append("find_phone")
    if is_duplicate:
        tags.append("duplicate")

    score = base + bonus
    # new_business cap (applied last): tiny but highly-rated → Tier 3 max
    if reviews < 10 and rating is not None and rating >= 4.5:
        tags.append("new_business")
        score = min(score, 54)
    score = max(0, min(100, score))

    return {
        "score": score,
        "tier": tier_for(score),
        "tags": tags,
        "breakdown": {"A": A, "B": B, "C": C, "D": D, "E": E, "bonus": bonus},
        "disqualified": disq,
        "web_status": ws,
    }


# ── tie-breaking (Step 6) — sort key for a ranked list ───────────────────────
def sort_key(rec):
    """Higher is better. rec must carry score, reviews, rating, web_status, phone."""
    ws_rank = 1 if rec.get("web_status") == "none" else 0
    has_phone = 1 if (rec.get("phone") or "").strip() else 0
    is_dup = 1 if "duplicate" in (rec.get("rank_tags") or []) else 0
    # duplicates sink to the bottom regardless of score
    return (-is_dup, rec.get("score", 0), rec.get("reviews", 0),
            rec.get("rating") or 0, ws_rank, has_phone)
