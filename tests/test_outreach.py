"""outreach.py — phone normalization, variant/competitor selection, message copy.

All offline: build_messages must work fully without OUTREACH_LLM.
"""
import pytest

from leadfinder import outreach, sql_gen


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.delenv("OUTREACH_LLM", raising=False)


# ── to_e164_in ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("phone,expected", [
    ("098862 74717", "919886274717"),      # trunk-0 mobile
    ("+91 98860 11111", "919886011111"),   # already country-coded
    ("9886011111", "919886011111"),        # bare 10-digit mobile
    ("919886011111", "919886011111"),      # passthrough
    ("91123456789", "91123456789"),        # 91 + 9-digit landline edge
    ("5551234567", None),                  # 10 digits but not 6-9 leading
    ("", None),
    (None, None),
    ("0000", None),                        # lstrip-to-empty edge
    ("12345", None),
])
def test_to_e164_in(phone, expected):
    assert outreach.to_e164_in(phone) == expected


# ── variant selection ────────────────────────────────────────────────────────
def test_pick_variant_priority():
    assert outreach.pick_variant({"rank_tags": "iconic_local;premium_zone"}) == "iconic"
    assert outreach.pick_variant({"rank_tags": ["upgrade_pitch"]}) == "upgrade"
    assert outreach.pick_variant({"rank_tags": "iconic_local;upgrade_pitch"}) == "iconic"
    assert outreach.pick_variant({"rank_tags": ""}) == "standard"
    assert outreach.pick_variant({}) == "standard"


# ── competitor selection ─────────────────────────────────────────────────────
LEAD = {"name": "Lead Gym", "phone": "98860 11111", "category": "Gym",
        "reviews": "25", "rating": "4.2",
        "address": "1 MG Rd, Bengaluru 560041, India"}


def test_pick_competitor_prefers_same_pin_over_reviews():
    same_pin = {"name": "Near Gym", "website": "https://near.com", "category": "Gym",
                "reviews": "50", "rating": "4.0",
                "address": "2 MG Rd, Bengaluru 560041, India"}
    far_big = {"name": "Far Gym", "website": "https://far.com", "category": "Gym",
               "reviews": "500", "rating": "4.9",
               "address": "9 Outer Rd, Bengaluru 560999, India"}
    assert outreach.pick_competitor(LEAD, [far_big, same_pin]) is same_pin


def test_pick_competitor_filters_websiteless():
    no_site = {"name": "X", "website": "", "category": "Gym", "reviews": "900",
               "address": "2 MG Rd, Bengaluru 560041, India"}
    assert outreach.pick_competitor(LEAD, [no_site]) is None


def test_pick_competitor_empty():
    assert outreach.pick_competitor(LEAD, []) is None


# ── message assembly ─────────────────────────────────────────────────────────
COMP = {"name": "Comp Gym", "website": "https://compgym.com", "category": "Gym",
        "reviews": "120", "rating": "4.5",
        "address": "2 MG Rd, Bengaluru 560041, India"}


def test_build_messages_shape():
    msgs = outreach.build_messages(LEAD, COMP, sector="gym")
    for key in ("lead_key", "variant", "competitor", "e164", "has_phone",
                "whatsapp", "call", "email", "walkin"):
        assert key in msgs
    assert msgs["whatsapp"]["text"]
    assert msgs["call"]["script"]
    assert msgs["email"]["subject"] and msgs["email"]["body"]
    assert msgs["email"]["mailto"].startswith("mailto:")
    assert msgs["walkin"]["opening"] and msgs["walkin"]["leave_behind"]


def test_build_messages_valid_phone_gets_wa_and_tel_urls():
    msgs = outreach.build_messages(LEAD, COMP)
    assert msgs["e164"] == "919886011111"
    assert msgs["has_phone"] is True
    assert msgs["whatsapp"]["url"].startswith("https://wa.me/919886011111?text=")
    assert msgs["call"]["tel"] == "tel:+919886011111"


def test_build_messages_invalid_phone_no_urls():
    lead = dict(LEAD, phone="12345")
    msgs = outreach.build_messages(lead, COMP)
    assert msgs["whatsapp"]["url"] is None
    assert msgs["call"]["tel"] is None
    assert msgs["has_phone"] is False


def test_build_messages_competitor_cited():
    msgs = outreach.build_messages(LEAD, COMP)
    assert "Comp Gym" in msgs["whatsapp"]["text"]
    assert msgs["competitor"]["reviews"] == 120


def test_build_messages_no_competitor_degrades_gracefully():
    msgs = outreach.build_messages(LEAD, None)
    assert msgs["competitor"] is None
    assert "Most of your competitors" in msgs["whatsapp"]["text"]


def test_build_messages_premium_clause_only_in_premium_zone():
    premium = dict(LEAD, rank_tags="premium_zone")
    assert "premium templates" in outreach.build_messages(premium, None)["whatsapp"]["text"]
    assert "premium templates" not in outreach.build_messages(LEAD, None)["whatsapp"]["text"]


def test_build_messages_lead_key_matches_sql_gen():
    msgs = outreach.build_messages(LEAD, None)
    assert msgs["lead_key"] == sql_gen.place_id_for(LEAD)


def test_build_messages_sender_signs_the_copy():
    msgs = outreach.build_messages(
        LEAD, None, sender={"name": "Bharath", "business": "Kunthive",
                            "phone": "98860 99999", "signoff": "Bharath @ Kunthive"})
    assert "Bharath @ Kunthive" in msgs["whatsapp"]["text"]
    assert "Bharath" in msgs["email"]["body"]
