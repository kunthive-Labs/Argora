"""scraper.py pure helpers — label stripping and the field-yield report.

Never calls scrape(): these run without Playwright or a network.
"""
import pytest

from leadfinder import scraper


# ── _strip_label ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("Phone: 080 2345 6789", "080 2345 6789"),
    ("Address: 12, MG Road, Bengaluru", "12, MG Road, Bengaluru"),
    ("Telefon: +49 30 123", "+49 30 123"),          # non-English label
    ("Téléphone: 01 23 45", "01 23 45"),
    ("+91 98860 11111", "+91 98860 11111"),          # no label → untouched
    ("12, MG Road", "12, MG Road"),                  # starts with digit → untouched
    ("", ""),
    (None, ""),
])
def test_strip_label(raw, expected):
    assert scraper._strip_label(raw) == expected


# ── is_captcha_page ──────────────────────────────────────────────────────────
# The retry loop and live-page checks in scrape() need a browser and are left
# untested here on purpose — this file covers pure parts only.
@pytest.mark.parametrize("url,content,expected", [
    ("https://www.google.com/sorry/index?continue=x", "", True),
    ("https://www.google.com/SORRY/index", "", True),                 # case-insensitive
    ("https://maps.google.com/maps/place/x", "", False),
    ("", "Our systems have detected unusual traffic from your computer network", True),
    ("", "We noticed UNUSUAL TRAFFIC FROM YOUR network", True),
    ("https://maps.google.com/maps/place/x",
     "Sorry Fried Chicken · 4.5 stars · unusually good traffic", False),
    ("", "", False),
    (None, None, False),
])
def test_is_captcha_page(url, content, expected):
    assert scraper.is_captcha_page(url, content) is expected


def test_scrape_error_carries_results_and_fatal_flag():
    e = scraper.ScrapeError("boom", [{"name": "A"}])
    assert e.results == [{"name": "A"}]
    assert e.fatal is False                       # default: this search only
    assert scraper.ScrapeError("wall", [], fatal=True).fatal is True


# ── field_yield ──────────────────────────────────────────────────────────────
def _place(**kw):
    rec = {"name": "Acme", "rating": "4.2", "reviews": "25", "category": "Gym",
           "address": "1 MG Rd", "phone": "98860", "website": "", "mapsUrl": "u"}
    rec.update(kw)
    return rec


def test_field_yield_empty():
    assert scraper.field_yield([]) == {}


def test_field_yield_fractions():
    results = [_place(), _place(phone=""), _place(phone=" "), _place()]
    fy = scraper.field_yield(results)
    assert fy["name"] == 1.0
    assert fy["phone"] == 0.5           # blanks and whitespace both count as missing
    assert "website" not in fy          # low website yield is the product working


def test_report_field_yield_warns_on_low_yield():
    results = [_place(phone="")] * 19 + [_place()]
    lines = []
    scraper.report_field_yield(results, log=lines.append)
    assert any(line.startswith("  field yield:") for line in lines)
    low = [line for line in lines if "LOW YIELD" in line]
    assert len(low) == 1
    assert "'phone'" in low[0] and "btn_phone" in low[0]


def test_report_field_yield_quiet_when_healthy():
    lines = []
    scraper.report_field_yield([_place()] * 20, log=lines.append)
    assert not any("LOW YIELD" in line for line in lines)
    assert len(lines) == 1              # just the summary line


def test_report_field_yield_no_warning_below_sample_floor():
    # 5 records, zero phones — too small a batch to call selector rot
    lines = []
    scraper.report_field_yield([_place(phone="")] * 5, log=lines.append)
    assert not any("LOW YIELD" in line for line in lines)
    assert len(lines) == 1


def test_report_field_yield_empty_is_silent():
    lines = []
    scraper.report_field_yield([], log=lines.append)
    assert lines == []
