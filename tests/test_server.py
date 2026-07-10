"""webapp/server.py — job orchestration, driven directly (no HTTP client).

scraper.scrape is monkeypatched throughout; nothing here opens a browser or
touches the network.
"""
import json
import os

import pytest
from fastapi import HTTPException

from leadfinder import scraper
from webapp import server


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the server's data dirs at tmp_path and reset the job slot."""
    raw, leads = tmp_path / "raw", tmp_path / "leads"
    raw.mkdir()
    leads.mkdir()
    monkeypatch.setattr(server, "RAW", str(raw))
    monkeypatch.setattr(server, "LEADS", str(leads))
    monkeypatch.setattr(server, "LAST_JOB", str(tmp_path / "last_job.json"))
    monkeypatch.setattr(server, "_current", {"job": None, "last": None})
    monkeypatch.delenv("ARGORA_FAKE_SCRAPE", raising=False)
    return tmp_path


def _rec(name="Acme Gym", website="", phone="98860 11111"):
    return {"name": name, "rating": "4.2", "reviews": "25", "category": "Gym",
            "website": website, "phone": phone,
            "address": "1 MG Rd, Bengaluru 560041",
            "mapsUrl": "https://maps.example/x"}


def _job():
    return server.Job("test-1", kind="scrape")


def _drain(job):
    """All SSE messages the job emitted."""
    out = []
    while not job.q.empty():
        out.append(job.q.get_nowait())
    return out


# ── _targets / _locations_of ─────────────────────────────────────────────────
def test_targets_expands_presets_and_customs():
    targets = server._targets(["driving-school"], ["Tutors ", " ", ""], None)
    assert targets[0][0] == "driving-school"
    assert targets[0][1] == "driving school"
    assert targets[0][3] == 10                    # preset review floor
    assert targets[1] == ("tutors", "Tutors", [], 0)
    assert len(targets) == 2                      # blank customs dropped


def test_targets_min_reviews_override_wins():
    targets = server._targets(["driving-school"], ["tutors"], 7)
    assert targets[0][3] == 7
    assert targets[1][3] == 7


def test_locations_of_batch_and_legacy():
    assert server._locations_of(server.RunReq(locations=["A", " B ", ""])) == ["A", "B"]
    assert server._locations_of(server.RunReq(location="X")) == ["X"]     # legacy caller
    assert server._locations_of(server.RunReq(locations=["A"], location="X")) == ["A"]
    assert server._locations_of(server.RunReq()) == []


# ── run_job ──────────────────────────────────────────────────────────────────
def test_run_job_happy_path(sandbox, monkeypatch):
    monkeypatch.setattr(server.scraper, "scrape",
                        lambda *a, **kw: [_rec(), _rec(name="Comp Gym",
                                                       website="https://comp.com",
                                                       phone="97753 22222")])
    job = _job()
    server.run_job(job, [], ["gyms"], ["HSR Layout"], 10, True, None, 30)

    assert job.error is None and job.done
    assert len(job.summary) == 1
    s = job.summary[0]
    assert (s["sector"], s["location"]) == ("gyms", "HSR Layout")
    assert s["leads"] == 1 and s["competitors"] == 1 and "error" not in s
    assert os.path.exists(os.path.join(server.LEADS, "gyms-hsr-layout-LEADS.csv"))
    with open(server.LAST_JOB, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted["ok"] is True and persisted["summary"] == job.summary
    assert _drain(job)[-1]["kind"] == "done"


def test_run_job_nonfatal_error_continues_to_next_target(sandbox, monkeypatch):
    calls = []

    def fake_scrape(query, *a, **kw):
        calls.append(query)
        if len(calls) == 1:
            raise scraper.ScrapeError("boom", [_rec()], fatal=False)
        return [_rec(name="Second")]

    monkeypatch.setattr(server.scraper, "scrape", fake_scrape)
    job = _job()
    server.run_job(job, [], ["a", "b"], ["HSR"], 10, True, None, 30)

    assert calls == ["a", "b"]                    # target 2 still ran
    assert job.error is None                      # whole job is NOT failed
    assert job.summary[0]["error"] == "boom"
    assert job.summary[0]["stem"] == "a-hsr-RECOVERED"
    assert "error" not in job.summary[1]
    assert os.path.exists(os.path.join(server.LEADS, "a-hsr-RECOVERED-LEADS.csv"))


def test_run_job_fatal_error_stops_everything(sandbox, monkeypatch):
    calls = []

    def fake_scrape(query, *a, **kw):
        calls.append(query)
        raise scraper.ScrapeError(scraper.CAPTCHA_STOP_MSG, [], fatal=True)

    monkeypatch.setattr(server.scraper, "scrape", fake_scrape)
    job = _job()
    server.run_job(job, [], ["a", "b"], ["HSR", "BTM"], 10, True, None, 30)

    assert calls == ["a"]                         # nothing after the wall
    assert job.error == scraper.CAPTCHA_STOP_MSG
    assert job.done                               # finally still ran
    assert any(m["kind"] == "error" for m in _drain(job))


def test_run_job_batch_areas(sandbox, monkeypatch):
    monkeypatch.setattr(server.scraper, "scrape", lambda *a, **kw: [_rec()])
    rests = []
    monkeypatch.setattr(server, "_area_rest",
                        lambda job, secs: rests.append(secs))
    job = _job()
    server.run_job(job, [], ["a", "b"], ["HSR", "BTM"], 10, True, None, 45)

    assert rests == [45]                          # one rest between two areas
    assert [(s["sector"], s["location"]) for s in job.summary] == [
        ("a", "HSR"), ("b", "HSR"), ("a", "BTM"), ("b", "BTM")]
    for stem in ("a-hsr", "b-hsr", "a-btm", "b-btm"):
        assert os.path.exists(os.path.join(server.LEADS, f"{stem}-LEADS.csv"))
    phases = [m for m in _drain(job) if m["kind"] == "phase"]
    assert phases[0]["area"] == 1 and phases[0]["areas"] == 2
    assert "area 1/2" in phases[0]["label"]


def test_run_job_stop_skips_remaining_areas(sandbox, monkeypatch):
    def fake_scrape(query, *a, should_stop=None, **kw):
        job.stop = True                           # user hits Halt mid-scrape
        return [_rec()]

    monkeypatch.setattr(server.scraper, "scrape", fake_scrape)
    job = _job()
    server.run_job(job, [], ["a"], ["HSR", "BTM"], 10, True, None, 30)

    assert len(job.summary) == 1                  # area 2 never scraped
    assert job.summary[0]["stem"].endswith("-RECOVERED")


def test_area_rest_is_stop_responsive(monkeypatch):
    job = _job()
    naps = []

    def fake_sleep(s):
        naps.append(s)
        job.stop = True

    monkeypatch.setattr(server.time, "sleep", fake_sleep)
    server._area_rest(job, 300)
    assert naps == [1]                            # bailed on the first tick


def test_fake_scrape_hook_bypasses_real_scraper(sandbox, monkeypatch):
    monkeypatch.setenv("ARGORA_FAKE_SCRAPE", "1")
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    def explode(*a, **kw):
        raise AssertionError("real scraper must not run under the fake hook")

    monkeypatch.setattr(server.scraper, "scrape", explode)
    job = _job()
    server.run_job(job, [], ["gyms"], ["HSR"], 8, True, None, 30)

    assert job.error is None
    assert job.summary[0]["scraped"] > 0
    assert job.summary[0]["leads"] > 0            # canned mix includes NO-SITE rows


# ── the single job slot ──────────────────────────────────────────────────────
def test_acquire_job_409_while_running(sandbox):
    first = server._acquire_job("scrape", {})
    with pytest.raises(HTTPException) as exc:
        server._acquire_job("extract", {})
    assert exc.value.status_code == 409
    first.done = True                             # slot frees on completion
    second = server._acquire_job("extract", {})
    assert second.id != first.id                  # seq suffix keeps ids unique
