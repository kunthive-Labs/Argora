"""outreach_log.py — the local touch log. All I/O redirected to tmp_path."""
import json
import logging

import pytest

from leadfinder import outreach_log


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(outreach_log, "DIR", str(tmp_path))
    monkeypatch.setattr(outreach_log, "LOG", str(tmp_path / "log.json"))


def test_load_missing_file_returns_empty():
    assert outreach_log.load() == {"version": 1, "touches": []}


def test_record_fills_defaults_and_monotonic_ids():
    t1 = outreach_log.record({"lead_key": "argora:abc", "channel": "call"})
    t2 = outreach_log.record({"lead_key": "argora:abc", "channel": "email"})
    assert t1["id"] == 1 and t2["id"] == 2
    assert t1["sent_at"]
    assert t1["outcome"] == "sent"
    assert t1["notes"] == ""
    assert t1["follow_up_at"] is None
    assert t1["pushed_to_db"] is False


def test_record_coerces_invalid_channel_and_outcome():
    t = outreach_log.record({"lead_key": "k", "channel": "carrier-pigeon",
                             "outcome": "ignored-me"})
    assert t["channel"] == "whatsapp"
    assert t["outcome"] == "sent"


def test_load_corrupt_file_warns_and_returns_empty(caplog):
    with open(outreach_log.LOG, "w", encoding="utf-8") as f:
        f.write("{not json")
    with caplog.at_level(logging.WARNING, logger="leadfinder.outreach_log"):
        data = outreach_log.load()
    assert data == {"version": 1, "touches": []}
    assert any("outreach log unreadable" in r.message for r in caplog.records)


def test_load_wrong_shape_returns_empty():
    with open(outreach_log.LOG, "w", encoding="utf-8") as f:
        json.dump(["not", "a", "dict"], f)
    assert outreach_log.load() == {"version": 1, "touches": []}


def test_touched_keys_groups_by_lead():
    outreach_log.record({"lead_key": "a", "channel": "call"})
    outreach_log.record({"lead_key": "a", "channel": "email"})
    outreach_log.record({"lead_key": "b", "channel": "call"})
    keys = outreach_log.touched_keys()
    assert len(keys["a"]) == 2
    assert len(keys["b"]) == 1


def test_last_touch():
    outreach_log.record({"lead_key": "a", "channel": "call",
                         "sent_at": "2026-01-01T10:00:00"})
    outreach_log.record({"lead_key": "a", "channel": "email",
                         "sent_at": "2026-02-01T10:00:00"})
    assert outreach_log.last_touch("a")["channel"] == "email"
    assert outreach_log.last_touch("missing") is None


def test_follow_ups_due_and_cleared_by_later_touch():
    outreach_log.record({"lead_key": "a", "channel": "call",
                         "sent_at": "2026-01-01T10:00:00",
                         "follow_up_at": "2026-01-03T10:00:00"})
    due = outreach_log.follow_ups_due(now="2026-01-05T00:00:00")
    assert len(due) == 1 and due[0]["lead_key"] == "a"

    # not yet due
    assert outreach_log.follow_ups_due(now="2026-01-02T00:00:00") == []

    # a later touch on the same lead clears the outstanding follow-up
    outreach_log.record({"lead_key": "a", "channel": "whatsapp",
                         "sent_at": "2026-01-04T10:00:00"})
    assert outreach_log.follow_ups_due(now="2026-01-05T00:00:00") == []
