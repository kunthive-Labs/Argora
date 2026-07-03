"""analyze.py — normalization, dedup, sector filtering, and the full pipeline."""
import pytest

from leadfinder import analyze, sql_gen


# ── column normalization ─────────────────────────────────────────────────────
def test_normalize_camelcase_aliases():
    rec = analyze.normalize({
        "title": "Acme Gym",
        "phoneNumber": "+91 98860 11111",
        "totalScore": "4.5",
        "reviewsCount": "1,234",
        "categoryName": "Gym",
        "fullAddress": "12 MG Rd, Bengaluru 560041, India",
        "googleMapsUrl": "https://maps.google.com/x",
    })
    assert rec["name"] == "Acme Gym"
    assert rec["phone"] == "+91 98860 11111"
    assert rec["rating"] == 4.5
    assert rec["reviews"] == 1234
    assert rec["category"] == "Gym"
    assert rec["address"] == "12 MG Rd, Bengaluru 560041, India"
    assert rec["maps_url"] == "https://maps.google.com/x"


def test_normalize_allcaps_aliases():
    rec = analyze.normalize({"name": "Acme", "PHONE": "98860", "WEBSITE": "acme.com",
                             "RATING": 4.0, "REVIEWS": 7})
    assert rec["phone"] == "98860"
    assert rec["website"] == "acme.com"
    assert rec["rating"] == 4.0
    assert rec["reviews"] == 7


@pytest.mark.parametrize("value,expected", [
    ("1,234", 1234), (None, 0), ("", 0), ("42", 42), (17, 17),
])
def test_to_int(value, expected):
    assert analyze._to_int(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("4.5", 4.5), ("junk", 0.0), (None, 0.0), (3, 3.0),
])
def test_to_float(value, expected):
    assert analyze._to_float(value) == expected


# ── dedup semantics ──────────────────────────────────────────────────────────
def _rec(name, phone):
    return analyze.normalize({"name": name, "phone": phone})


def test_dedupe_same_name_same_phone_case_insensitive():
    out = analyze.dedupe([_rec("Acme Gym", "98860"), _rec("ACME GYM", "98860")])
    assert len(out) == 1


def test_dedupe_same_name_no_phone():
    out = analyze.dedupe([_rec("Acme Gym", ""), _rec("acme gym", "")])
    assert len(out) == 1


def test_dedupe_same_name_different_phones_both_kept():
    out = analyze.dedupe([_rec("Acme Gym", "98860"), _rec("Acme Gym", "97753")])
    assert len(out) == 2


# ── sector filter + postal ───────────────────────────────────────────────────
def test_matches_sector_excludes_case_insensitive():
    r = {"category": "Gym Equipment Shop", "name": "X"}
    assert analyze.matches_sector(r, ["equipment"]) is False
    assert analyze.matches_sector(r, ["yoga"]) is True


def test_postal_six_digits_only():
    assert analyze._postal("12 MG Rd, Bengaluru 560041, India") == "560041"
    assert analyze._postal("12 Main St, Springfield 62704, USA") == ""
    assert analyze._postal("") == ""


def test_has_real_website_delegates_to_ranking():
    assert analyze.has_real_website("https://sites.google.com/view/x") is False
    assert analyze.has_real_website("https://facebook.com/x") is False
    assert analyze.has_real_website("https://acme.in") is True


# ── full pipeline ────────────────────────────────────────────────────────────
FIXTURE = [
    {"name": "Lead Gym", "phone": "98860 11111", "website": "",
     "category": "Gym", "rating": 4.2, "reviews": 25,
     "address": "1 MG Rd, Bengaluru 560041, India"},
    {"name": "Comp Gym", "phone": "98860 22222", "website": "https://compgym.com",
     "category": "Gym", "rating": 4.5, "reviews": 100,
     "address": "2 MG Rd, Bengaluru 560041, India"},
    {"name": "Ghost Gym", "phone": "", "website": "",
     "category": "Gym", "rating": 5.0, "reviews": 2,
     "address": "3 MG Rd, Bengaluru 560041, India"},
    {"name": "Twin A", "phone": "98860 33333", "website": "",
     "category": "Gym", "rating": 4.0, "reviews": 40,
     "address": "4 MG Rd, Bengaluru 560041, India"},
    {"name": "Twin B", "phone": "98860 33333", "website": "",
     "category": "Gym", "rating": 4.0, "reviews": 30,
     "address": "5 MG Rd, Bengaluru 560041, India"},
]


def test_analyze_splits_leads_and_competitors():
    allrec, leads, comps = analyze.analyze(FIXTURE)
    lead_names = {r["name"] for r in leads}
    assert "Lead Gym" in lead_names
    assert "Comp Gym" not in lead_names           # has a real site
    assert "Ghost Gym" not in lead_names          # disqualified (no phone, 2 reviews)
    assert {r["name"] for r in comps} == {"Comp Gym"}
    assert len(allrec) == 5


def test_analyze_shared_phone_tags_duplicates_and_sinks_them():
    _, leads, _ = analyze.analyze(FIXTURE)
    twins = [r for r in leads if r["name"].startswith("Twin")]
    assert len(twins) == 2
    assert all("duplicate" in r["rank_tags"] for r in twins)
    assert leads[0]["name"] == "Lead Gym"         # non-duplicate ranks above twins


def test_analyze_min_reviews_floor():
    _, leads, _ = analyze.analyze(FIXTURE, min_reviews=35)
    assert {r["name"] for r in leads} == {"Twin A"}


def test_analyze_exclude_keywords():
    _, leads, _ = analyze.analyze(FIXTURE, exclude=["lead"])
    assert "Lead Gym" not in {r["name"] for r in leads}


# ── UTF-8 round trip (regression for Windows cp1252 default) ─────────────────
def test_csv_roundtrip_preserves_utf8(tmp_path):
    allrec, leads, _ = analyze.analyze([
        {"name": "Café Müller ✓", "phone": "98860 11111", "website": "",
         "category": "Cafe", "rating": 4.4, "reviews": 60,
         "address": "12 MG Rd, Bengaluru 560041, India"},
    ])
    path = tmp_path / "utf8-LEADS.csv"
    analyze.write_csv(str(path), leads)
    rows = sql_gen.load_csv(str(path))
    assert rows[0]["name"] == "Café Müller ✓"
