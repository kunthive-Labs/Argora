"""sql_gen.py — field derivation, SQL literal escaping, and the idempotent INSERT."""
import pytest

from leadfinder import sql_gen


# ── SQL literal escaping ─────────────────────────────────────────────────────
def test_s_null_cases():
    assert sql_gen._s(None) == "NULL"
    assert sql_gen._s("") == "NULL"


def test_s_quotes_doubled():
    assert sql_gen._s("O'Brien") == "'O''Brien'"


def test_s_injection_shaped_input_stays_literal():
    out = sql_gen._s("x'); DROP TABLE leads;--")
    assert out == "'x''); DROP TABLE leads;--'"      # quote doubled, one literal


def test_jsonb_escapes_quotes():
    out = sql_gen._jsonb({"note": "it's fine"})
    assert out.endswith("::jsonb")
    assert "''" in out


def test_text_array():
    assert sql_gen._text_array([]) == "ARRAY[]::text[]"
    assert sql_gen._text_array(["a", "b'c"]) == "ARRAY['a','b''c']::text[]"


# ── field derivation ─────────────────────────────────────────────────────────
def test_norm_phone():
    assert sql_gen.norm_phone("+91 98860 11111") == "919886011111"
    assert sql_gen.norm_phone(None) == ""
    assert sql_gen.norm_phone("(080) 2345-6789") == "08023456789"


def test_extract_place_id_query_param_urldecoded():
    url = "https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJabc%3D"
    assert sql_gen.extract_place_id(url) == "ChIJabc="


def test_extract_place_id_hex_pair():
    url = "https://www.google.com/maps/place/x/data=!1s0x3bae15c8f:0x9e01dd3b0!8m2"
    assert sql_gen.extract_place_id(url) == "0x3bae15c8f:0x9e01dd3b0"


def test_extract_place_id_empty():
    assert sql_gen.extract_place_id("") == ""
    assert sql_gen.extract_place_id("https://example.com") == ""


def test_synth_place_id_deterministic():
    a = sql_gen.synth_place_id("Acme", "98860")
    b = sql_gen.synth_place_id("Acme", "98860")
    c = sql_gen.synth_place_id("Acme", "97753")
    assert a == b
    assert a != c
    assert a.startswith("argora:")


@pytest.mark.parametrize("website,expected", [
    ("", ("none", "")),
    ("N/A", ("none", "")),
    ("https://sites.google.com/view/x", ("none", "")),
    ("https://instagram.com/x", ("social", "https://instagram.com/x")),
    ("https://acme.com", ("real", "https://acme.com")),
])
def test_classify_website(website, expected):
    assert sql_gen.classify_website(website) == expected


def test_social_links():
    assert sql_gen.social_links("https://facebook.com/x", "social") == \
        {"facebook": "https://facebook.com/x"}
    assert sql_gen.social_links("https://odd.example", "social") == \
        {"other": "https://odd.example"}
    assert sql_gen.social_links("https://acme.com", "real") == {}


def test_extract_coords():
    url = "https://www.google.com/maps/place/x/@12.9716,77.5946,17z/data=..."
    assert sql_gen.extract_coords(url) == ("12.9716", "77.5946")
    neg = "https://www.google.com/maps/place/x/@-33.8688,151.2093,15z"
    assert sql_gen.extract_coords(neg) == ("-33.8688", "151.2093")
    assert sql_gen.extract_coords("https://example.com") == (None, None)


def test_clean_area_strips_pin_and_country():
    area = sql_gen.clean_area("12 MG Rd, Bengaluru 560041, India", "560041")
    assert area == "12 MG Rd, Bengaluru"


def test_score_0_100():
    assert sql_gen.score_0_100(5, 999) == 100
    assert sql_gen.score_0_100(0, 0) == 0
    assert sql_gen.score_0_100("bad", "x") is None


def test_tags_from_csv():
    assert sql_gen._tags_from_csv("a;b") == ["a", "b"]
    assert sql_gen._tags_from_csv(["a", "b"]) == ["a", "b"]
    assert sql_gen._tags_from_csv("") == []
    assert sql_gen._tags_from_csv(None) == []


# ── generate: the idempotent INSERT ──────────────────────────────────────────
def _row(name="Acme Gym", phone="98860 11111", website="", maps_url="", **kw):
    row = {"name": name, "phone": phone, "website": website, "maps_url": maps_url,
           "category": "Gym", "rating": "4.2", "reviews": "25",
           "address": "1 MG Rd, Bengaluru 560041, India",
           "website_status": "NO-SITE"}
    row.update(kw)
    return row


def test_generate_dedupes_by_normalized_phone():
    rows = [_row(name="A", phone="98860 11111"),
            _row(name="B", phone="9886011111")]      # same digits
    sql, n = sql_gen.generate(rows, "argora/test")
    assert n == 1


def test_generate_dedupes_by_place_id():
    url = "https://www.google.com/maps/place/x/data=!1s0xdead1:0xbeef2"
    rows = [_row(name="A", phone="98860 11111", maps_url=url),
            _row(name="B", phone="97753 22222", maps_url=url)]
    sql, n = sql_gen.generate(rows, "argora/test")
    assert n == 1


def test_generate_only_leads_drops_real_sites():
    rows = [_row(), _row(name="Comp", phone="97753 22222",
                         website="https://compgym.com", website_status="has-site")]
    sql, n = sql_gen.generate(rows, "argora/test", only_leads=True)
    assert n == 1
    assert "Comp" not in sql


def test_generate_guards_present():
    sql, n = sql_gen.generate([_row()], "argora/test")
    assert n == 1
    assert "ON CONFLICT (source_place_id) DO NOTHING" in sql
    assert "WHERE NOT EXISTS" in sql
    assert "INSERT INTO leads" in sql


def test_generate_empty_input():
    assert sql_gen.generate([], "argora/test") == ("-- no new rows to insert\n", 0)


def test_generate_escapes_apostrophes():
    sql, n = sql_gen.generate([_row(name="O'Brien Gym")], "argora/test")
    assert n == 1
    assert "O''Brien Gym" in sql


def test_generate_dataset_lands_in_sql():
    sql, _ = sql_gen.generate([_row()], "argora/gym-jayanagar")
    assert "'argora/gym-jayanagar'" in sql
