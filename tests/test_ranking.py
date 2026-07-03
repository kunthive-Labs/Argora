"""ranking.py — the 5-dimension scoring algorithm. Pure functions, no I/O.

These tests pin the spec (KunthiveOS/docs/handover-lead-ranking-algorithm.md)
so the Python side can't silently drift from the TypeScript twin.
"""
import pytest

from leadfinder import ranking


# ── web_status ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("website,expected", [
    ("", "none"),
    (None, "none"),
    ("none", "none"),
    ("N/A", "none"),
    ("-", "none"),
    ("https://sites.google.com/view/acme", "none"),      # Google Site = no site
    ("https://acme.business.site", "none"),              # GBP auto-site = no site
    ("https://facebook.com/acme", "social"),
    ("https://instagram.com/acme", "social"),
    ("https://acme.com", "real"),
    ("https://acme.co.in/about", "real"),
    ("not a url", "none"),                               # no domain-looking thing
])
def test_web_status(website, expected):
    assert ranking.web_status(website) == expected


def test_is_google_site():
    assert ranking.is_google_site("https://sites.google.com/site/x")
    assert ranking.is_google_site("https://acme.business.site")
    assert not ranking.is_google_site("https://acme.com")
    assert not ranking.is_google_site("")


# ── dimension boundaries (via rank breakdown) ────────────────────────────────
@pytest.mark.parametrize("reviews,expected", [
    (0, 0), (1, 5), (10, 5), (11, 10), (50, 10),
    (51, 16), (150, 16), (151, 21), (400, 21), (401, 25),
])
def test_review_volume_boundaries(reviews, expected):
    res = ranking.rank(name="X", phone="98860 11111", reviews=reviews)
    assert res["breakdown"]["B"] == expected


@pytest.mark.parametrize("rating,expected", [
    (None, 6), (0, 6),          # new / unrated → neutral, not penalised
    (2.9, 0), (3.0, 4), (3.4, 4), (3.5, 8), (4.0, 12), (4.4, 12), (4.5, 15), (5.0, 15),
])
def test_rating_trust_boundaries(rating, expected):
    res = ranking.rank(name="X", phone="98860 11111", reviews=50, rating=rating)
    assert res["breakdown"]["C"] == expected


def test_web_gap_and_reachability():
    none_ = ranking.rank(name="X", phone="98860 11111", website="")
    social = ranking.rank(name="X", phone="98860 11111", website="facebook.com/x")
    real = ranking.rank(name="X", phone="98860 11111", website="https://acme.com")
    assert none_["breakdown"]["A"] == 40
    assert social["breakdown"]["A"] == 25
    assert real["breakdown"]["A"] == 0
    no_phone = ranking.rank(name="X", phone="", reviews=50)
    assert none_["breakdown"]["D"] == 10
    assert no_phone["breakdown"]["D"] == 0


# ── hard disqualification ────────────────────────────────────────────────────
@pytest.mark.parametrize("name,phone,rating,reviews,expected", [
    ("Acme", "", None, 4, True),           # ghost: no phone, <5 reviews
    ("Acme", "", None, 5, False),
    ("Acme", "98860", 2.4, 20, True),      # unhappy customers
    ("Acme", "98860", 2.4, 19, False),
    ("Acme (Permanently Closed)", "98860", 4.5, 100, True),
    ("Acme", "", None, 0, True),           # no signal at all
])
def test_is_disqualified(name, phone, rating, reviews, expected):
    assert ranking.is_disqualified(name, phone, rating, reviews) is expected


# ── tiers ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("score,tier", [
    (100, "hot"), (75, "hot"), (74, "warm"), (55, "warm"),
    (54, "cool"), (35, "cool"), (34, "cold"), (0, "cold"),
])
def test_tier_boundaries(score, tier):
    assert ranking.tier_for(score) == tier


# ── special scenarios ────────────────────────────────────────────────────────
def test_upgrade_pitch_tag():
    res = ranking.rank(name="X", phone="98860 11111",
                       website="facebook.com/x", reviews=150)
    assert "upgrade_pitch" in res["tags"]
    assert res["breakdown"]["bonus"] == 5


def test_iconic_local_tag():
    res = ranking.rank(name="X", phone="98860 11111", website="", reviews=400)
    assert "iconic_local" in res["tags"]


def test_premium_zone_bonus():
    res = ranking.rank(name="X", phone="98860 11111", reviews=50, postal="560001")
    assert "premium_zone" in res["tags"]
    assert res["breakdown"]["bonus"] == 3
    plain = ranking.rank(name="X", phone="98860 11111", reviews=50, postal="560999")
    assert "premium_zone" not in plain["tags"]


def test_find_phone_tag_no_bonus():
    res = ranking.rank(name="X", phone="", reviews=250)
    assert "find_phone" in res["tags"]
    assert res["breakdown"]["bonus"] == 0


def test_duplicate_tag():
    res = ranking.rank(name="X", phone="98860 11111", reviews=50, is_duplicate=True)
    assert "duplicate" in res["tags"]


def test_new_business_cap():
    res = ranking.rank(name="X", phone="98860 11111", reviews=5, rating=4.8)
    assert "new_business" in res["tags"]
    assert res["score"] <= 54
    assert res["tier"] in ("cool", "cold")


def test_score_clamped_to_100():
    res = ranking.rank(name="X", phone="98860 11111", website="",
                       category="real estate", postal="560001",
                       rating=4.9, reviews=500)
    assert res["score"] == 100


# ── tie-breaking ─────────────────────────────────────────────────────────────
def test_duplicates_sink_regardless_of_score():
    dup = {"score": 90, "reviews": 500, "rating": 4.9, "web_status": "none",
           "phone": "98860", "rank_tags": ["duplicate"]}
    fresh = {"score": 40, "reviews": 10, "rating": 3.5, "web_status": "none",
             "phone": "98860", "rank_tags": []}
    ordered = sorted([dup, fresh], key=ranking.sort_key, reverse=True)
    assert ordered[0] is fresh
