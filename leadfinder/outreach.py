"""
outreach.py — turn a ranked LEAD into ready-to-send, multi-channel pitch copy.

The whole point of Argora is no-website prospecting: a business that has no real
website while its same-category neighbours do. This module writes the message
that says exactly that — "your neighbour {competitor} has a website and N
reviews; you don't" — across WhatsApp, a phone-call script, an email draft, and
a walk-in opener.

Design, mirroring ranking.py / sql_gen.py:
  * pure functions, no I/O, no network — unit-testable on the CSVs already on disk
  * the lead key, phone-digit stripping, and postal parsing are REUSED from
    sql_gen so Argora's key matches what lands in KunthiveOS (never reinvented)
  * message variants are picked from the lead's rank_tags (upgrade / iconic /
    standard) so the pitch fits the situation instead of one fixed template
  * an optional Claude polish is a no-op stub gated behind OUTREACH_LLM=1, so the
    feature never *requires* a network call

Public entry points:
    pick_competitor(lead, competitors) -> dict | None
    build_messages(lead, competitor, *, sector="", sender=None) -> dict
    to_e164_in(phone) -> str | None        # '919886011111' (no '+')
"""
import os
import urllib.parse

from leadfinder import sql_gen

# A neutral default identity. The UI overrides this per-browser (localStorage),
# so rebranding never needs a code change.
DEFAULT_SENDER = {
    "name": "",            # the human reaching out, e.g. "Bharath"
    "business": "Kunthive",
    "phone": "",
    "signoff": "Kunthive",
}


# ── phone → E.164 (India) ────────────────────────────────────────────────────
def to_e164_in(phone):
    """Indian mobile/landline → '91XXXXXXXXXX' (no '+', the form wa.me wants),
    or None if it can't be made into a sane number. tel: links add the '+'.

    Reuses sql_gen.norm_phone for digit-stripping; we never touch norm_phone
    itself (it must stay digits-only for the DB dedup key)."""
    digits = sql_gen.norm_phone(phone)          # e.g. '098862 74717' -> '09886274717'
    if not digits:
        return None
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) == 11 and digits.startswith("91"):  # 91 + 9-digit landline edge
        return digits
    return None


# ── competitor selection ─────────────────────────────────────────────────────
def _locality(rec):
    """Best-effort locality: the CSV `area` is often blank, so fall back to the
    6-digit PIN parsed from the address (single-source via sql_gen)."""
    return (rec.get("area") or "").strip() or sql_gen.extract_postal(
        rec.get("address", ""))


def _postal(rec):
    return sql_gen.extract_postal(rec.get("address", ""))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _category_overlap(a, b):
    """Loose category match: either contains the other's first word. The
    COMPETITORS file is already category-homogeneous per run, so this mostly
    filters the odd mis-tagged row."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return True
    return a in b or b in a or a.split()[0] == b.split()[0]


def pick_competitor(lead, competitors):
    """Choose the most persuasive site-having competitor to cite for `lead`.

    Priority: same category → prefer same PIN/locality ("your neighbour") →
    most reviews, then highest rating (a credible "they have a site AND N
    reviews" anchor). Returns None when there's nothing to cite (the message
    then degrades to a generic web-gap pitch)."""
    if not competitors:
        return None
    lead_cat = lead.get("category", "")
    lead_pin = _postal(lead)

    pool = [c for c in competitors
            if c.get("website") and _category_overlap(lead_cat, c.get("category", ""))]
    if not pool:
        pool = [c for c in competitors if c.get("website")]
    if not pool:
        return None

    def key(c):
        same_pin = 1 if (lead_pin and _postal(c) == lead_pin) else 0
        return (same_pin, _num(c.get("reviews")), _num(c.get("rating")))

    return max(pool, key=key)


# ── message variant ──────────────────────────────────────────────────────────
def _tags(lead):
    t = lead.get("rank_tags", "")
    if isinstance(t, list):
        return set(t)
    return {x for x in str(t).split(";") if x}


def pick_variant(lead):
    """The primary pitch angle, from the lead's rank_tags (priority order)."""
    tags = _tags(lead)
    if "iconic_local" in tags:          # no site, 300+ reviews — lead with rep
        return "iconic"
    if "upgrade_pitch" in tags:         # social-only, 100+ reviews — upgrade them
        return "upgrade"
    return "standard"


# ── templates ────────────────────────────────────────────────────────────────
# Module-level format strings, keyed [channel][variant]. Hand-edit freely.
# Available fields: name, biz, reviews, rating, locality, comp (competitor name),
# comp_reviews, sender, sender_phone, signoff, comp_clause, premium_clause.
WHATSAPP = {
    "standard": (
        "Hi {name} 👋 I came across your {biz} on Google Maps — {rating}★ with "
        "{reviews} reviews, genuinely strong. One thing stood out: you don't have "
        "a website yet.{comp_clause} When people search, you're handing them to "
        "whoever shows up with a site. I build clean, fast websites for local "
        "businesses like yours in about a week. Worth a quick chat?{premium_clause}\n\n— {signoff}"),
    "upgrade": (
        "Hi {name} 👋 Your {biz} has a great following — {reviews} reviews — but "
        "the only link Google shows is a social page.{comp_clause} A proper "
        "website would put all of that under your own name and rank you on "
        "search. I set these up for local businesses in about a week. Open to a "
        "quick chat?{premium_clause}\n\n— {signoff}"),
    "iconic": (
        "Hi {name} 👋 You're one of the most-reviewed {biz}s in {locality} "
        "({reviews} reviews) — and yet there's no website when people look you "
        "up.{comp_clause} You've earned the reputation; a website just makes it "
        "findable. I build them for local businesses in about a week. Worth a "
        "quick chat?{premium_clause}\n\n— {signoff}"),
}

CALL_SCRIPT = {
    "standard": (
        "CALL SCRIPT — {name} ({biz})\n"
        "• Open: \"Hi, am I speaking with someone from {name}? I help local "
        "businesses get a proper website — saw you're {rating}★ with {reviews} "
        "reviews but don't have a site yet.\"\n"
        "• Hook:{comp_line} \"When people Google your category, they land on "
        "whoever has a website — that's business walking past you.\"\n"
        "• Offer: \"I build a clean, mobile-friendly site in about a week, and I "
        "handle everything — you just send photos.\"\n"
        "• Ask: \"Can I send a sample on WhatsApp and we take it from there?\"\n"
        "• If busy: get the best time to call back."),
    "upgrade": (
        "CALL SCRIPT — {name} ({biz})\n"
        "• Open: \"Hi, is this {name}? You've got {reviews} reviews — really "
        "strong — but the only link online is a social page.\"\n"
        "• Hook:{comp_line} \"A website puts your name on Google search, not just "
        "social, and you own it.\"\n"
        "• Offer: \"I set one up in about a week, fully done-for-you.\"\n"
        "• Ask: \"Can I WhatsApp you a sample?\"\n"
        "• If busy: get a callback time."),
    "iconic": (
        "CALL SCRIPT — {name} ({biz})\n"
        "• Open: \"Hi, is this {name}? You're one of the best-known in "
        "{locality} — {reviews} reviews — but there's no website when people "
        "look you up.\"\n"
        "• Hook:{comp_line} \"You've earned the name; a site just makes it "
        "findable and bookable.\"\n"
        "• Offer: \"I build it in about a week, done-for-you.\"\n"
        "• Ask: \"Can I send a sample on WhatsApp?\"\n"
        "• If busy: get a callback time."),
}

EMAIL_SUBJECT = {
    "standard": "A website for {name}?",
    "upgrade": "{name} — from social page to your own website",
    "iconic": "{name} has the reputation — but no website",
}

EMAIL_BODY = {
    "standard": (
        "Hi {name} team,\n\n"
        "I came across your {biz} on Google Maps — {rating}★ across {reviews} "
        "reviews, which is excellent. I noticed you don't have a website yet."
        "{comp_clause}\n\n"
        "When people search for your services, they tend to go with whoever shows "
        "up with a proper website. I build clean, fast, mobile-friendly sites for "
        "local businesses — usually live in about a week, fully done-for-you.\n\n"
        "Could I send over a quick sample?\n\n"
        "Best,\n{sender_block}"),
    "upgrade": (
        "Hi {name} team,\n\n"
        "Your {biz} has built up {reviews} reviews — genuinely strong — but the "
        "only presence online is a social page.{comp_clause}\n\n"
        "A website would put everything under your own name, rank you on Google "
        "search, and give customers one place to find and contact you. I build "
        "these for local businesses, live in about a week.\n\n"
        "Could I send a quick sample?\n\n"
        "Best,\n{sender_block}"),
    "iconic": (
        "Hi {name} team,\n\n"
        "You're one of the most-reviewed {biz}s in {locality} ({reviews} "
        "reviews) — yet there's no website when people look you up.{comp_clause}\n\n"
        "You've already earned the reputation; a website simply makes it findable "
        "and bookable. I build them for local businesses, live in about a week.\n\n"
        "Could I send a quick sample?\n\n"
        "Best,\n{sender_block}"),
}

WALKIN_OPENING = {
    "standard": (
        "Hi — I work with local businesses on their websites. I saw {name} has "
        "{reviews} reviews on Google but no website yet.{comp_short} Mind if I "
        "show you what one could look like?"),
    "upgrade": (
        "Hi — I noticed {name} has a big following online ({reviews} reviews) but "
        "only a social page, no website.{comp_short} Can I show you a quick "
        "sample?"),
    "iconic": (
        "Hi — {name} is one of the best-known names around here, but there's no "
        "website when people search.{comp_short} Mind if I show you what one "
        "could look like?"),
}


# ── message assembly ─────────────────────────────────────────────────────────
def _biz_noun(lead, sector):
    """A readable noun for the business type."""
    return (lead.get("category") or sector or "business").strip().lower()


def _sender(sender):
    s = dict(DEFAULT_SENDER)
    if sender:
        s.update({k: v for k, v in sender.items() if v})
    s["signoff"] = s.get("signoff") or s.get("business") or "Kunthive"
    return s


def _sender_block(s):
    lines = [x for x in (s.get("name"), s.get("business"), s.get("phone")) if x]
    return "\n".join(lines) if lines else s["signoff"]


def _clauses(lead, competitor):
    """Build the competitor-citing fragments used across channels."""
    if competitor:
        comp = competitor.get("name", "").strip()
        comp_reviews = int(_num(competitor.get("reviews")))
        rev = f" with {comp_reviews} reviews" if comp_reviews else ""
        comp_clause = (f" Meanwhile a neighbour like {comp} already has a "
                       f"website{rev} — and that's who customers find first.")
        comp_line = (f" Mention {comp} has a website{rev}.")
        comp_short = f" {comp} nearby already has one."
    else:
        comp_clause = (" Most of your competitors already have one — and that's "
                       "who customers find first.")
        comp_line = " Note most competitors already have a website."
        comp_short = " Most of your competitors already have one."
    return comp_clause, comp_line, comp_short


def build_messages(lead, competitor, *, sector="", sender=None):
    """Return every channel's copy for one lead, plus launch URLs.

    Shape:
      { lead_key, variant, competitor: {name,reviews,website}|None,
        e164, has_phone,
        whatsapp: {text, url|None},
        call:     {tel|None, script},
        email:    {subject, body, mailto},
        walkin:   {opening, leave_behind} }
    """
    s = _sender(sender)
    variant = pick_variant(lead)
    tags = _tags(lead)
    biz = _biz_noun(lead, sector)
    locality = _locality(lead) or "your area"
    e164 = to_e164_in(lead.get("phone", ""))
    comp_clause, comp_line, comp_short = _clauses(lead, competitor)
    premium_clause = (" (I keep a few premium templates for established names too.)"
                      if "premium_zone" in tags else "")

    ctx = {
        "name": lead.get("name", "there").strip() or "there",
        "biz": biz,
        "reviews": int(_num(lead.get("reviews"))),
        "rating": lead.get("rating") or "—",
        "locality": locality,
        "signoff": s["signoff"],
        "sender_block": _sender_block(s),
        "comp_clause": comp_clause,
        "comp_line": comp_line,
        "comp_short": comp_short,
        "premium_clause": premium_clause,
    }

    wa_text = WHATSAPP[variant].format(**ctx)
    wa_url = None
    if e164:
        wa_url = f"https://wa.me/{e164}?text={urllib.parse.quote(wa_text)}"

    call_script = CALL_SCRIPT[variant].format(**ctx)
    tel = f"tel:+{e164}" if e164 else None

    subject = EMAIL_SUBJECT[variant].format(**ctx)
    body = EMAIL_BODY[variant].format(**ctx)
    mailto = ("mailto:?subject=" + urllib.parse.quote(subject)
              + "&body=" + urllib.parse.quote(body))

    walkin_open = WALKIN_OPENING[variant].format(**ctx)
    leave_behind = (f"{ctx['name']} — no website yet. {s['signoff']} builds local "
                    f"business sites in ~1 week. {s.get('phone') or ''}").strip()

    messages = {
        "lead_key": sql_gen.place_id_for(lead),
        "variant": variant,
        "competitor": ({"name": competitor.get("name", ""),
                        "reviews": int(_num(competitor.get("reviews"))),
                        "website": competitor.get("website", "")}
                       if competitor else None),
        "e164": e164,
        "has_phone": bool(e164),
        "whatsapp": {"text": wa_text, "url": wa_url},
        "call": {"tel": tel, "script": call_script},
        "email": {"subject": subject, "body": body, "mailto": mailto},
        "walkin": {"opening": walkin_open, "leave_behind": leave_behind},
    }
    return llm_rewrite(messages)


# ── optional Claude polish (off by default) ──────────────────────────────────
def llm_rewrite(messages):
    """Slot for an optional Claude polish of the generated copy. Disabled unless
    OUTREACH_LLM=1, and even then this stub returns the messages unchanged — the
    feature must work fully offline. Wire the Anthropic Messages API here later
    if you want the copy rephrased; keep it behind the env flag."""
    if os.environ.get("OUTREACH_LLM") != "1":
        return messages
    # Intentionally a no-op for now. (Future: call Claude, swap in .text fields.)
    return messages
