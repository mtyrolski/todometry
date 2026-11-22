"""Activity sync keeps its cache across repeated and interrupted fetches."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.unit.database.helpers import event_payload, make_event
from todometry.core.utils import Cache
from todometry.database.db_activity import DatabaseActivity
from todometry.features import data_sync
from todometry.features.activity import load_activity_cache, merge_activity_cache
from todometry.features.data_sync import (
    activity_history_status,
    backfill_activity_history,
    sync_recent_activity,
)


@pytest.fixture(autouse=True)
def isolate_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("TODOIST_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(data_sync, "_now_utc", lambda: utc("2026-11-22T12:00:00"))


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_recent_sync_merges_and_deduplicates(monkeypatch, tmp_path):
    previous = make_event(
        "previous", date=datetime(2026, 11, 20, 10, microsecond=250000)
    )
    added = make_event("added", date=datetime(2026, 11, 22, 10))
    cache = Cache(str(tmp_path / "source"))
    cache.activity.save({previous})

    db = DatabaseActivity()

    def fetch(existing, *, date_from, date_to):
        assert existing == {previous}
        assert date_from == utc("2026-11-19T00:00:00")
        assert date_to == utc("2026-11-22T12:00:00")
        return [previous, added]

    monkeypatch.setattr(db, "fetch_activity_incremental", fetch)
    result = sync_recent_activity(db, cache=cache)

    assert result.new_events == {added}
    assert result.events == {previous, added}
    assert load_activity_cache(cache=cache) == result.events
    assert load_activity_cache() == set()


def test_history_checkpoint_survives_interrupted_scan(monkeypatch):
    checkpoint = make_event("checkpoint", date=datetime(2026, 11, 16, 10))

    def fetch(**kwargs):
        kwargs["on_window"](
            utc("2026-11-16T00:00:00"), utc("2026-11-20T00:00:00"), {checkpoint}
        )
        raise RuntimeError("network interrupted")

    db = DatabaseActivity()
    monkeypatch.setattr(db, "fetch_activity_history", fetch)
    with pytest.raises(RuntimeError, match="network interrupted"):
        backfill_activity_history(db)

    assert load_activity_cache() == {checkpoint}
    history = activity_history_status()
    assert history["days"]["2026-11-18"] == {"coverage": "covered", "eventCount": 0}
    assert history["days"]["2026-11-20"]["coverage"] == "unverified"
    assert history["lastSyncedAt"] is None


def test_history_result_reports_new_events_after_checkpoint(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    known = make_event("known", date=now)
    older = make_event("older", date=now - timedelta(days=1))
    cache = Cache(str(tmp_path / "source"))
    cache.activity.save({known})
    db = DatabaseActivity()
    pages = iter([[older], []])
    monkeypatch.setattr(db, "_fetch_activity_range", lambda **_kwargs: next(pages))

    result = backfill_activity_history(
        db, cache=cache, nweeks_window_size=1, early_stop_after_n_windows=1
    )

    assert result.new_events == {older}
    assert result.events == load_activity_cache(cache=cache) == {known, older}
    assert load_activity_cache() == set()


def test_activity_status_distinguishes_legacy_events_from_verified_quiet_days(
    monkeypatch,
):
    monkeypatch.setattr(data_sync, "_now_utc", lambda: utc("2026-03-30T08:00:00"))
    previous = make_event("legacy", date=datetime(2026, 3, 26, 23, 30))
    Cache().activity.save({previous})
    db = DatabaseActivity()
    monkeypatch.setattr(db, "fetch_activity_incremental", lambda *_args, **_kwargs: [])

    tz = "Europe/Warsaw"
    legacy = activity_history_status(timezone_name=tz)
    assert legacy["firstEventDate"] == "2026-03-27"
    assert legacy["coverageStartDate"] is None
    assert legacy["lastSyncedAt"] is None
    assert legacy["days"]["2026-03-27"] == {"coverage": "unverified", "eventCount": 1}

    sync_recent_activity(db)
    confirmed = activity_history_status(date(2026, 3, 1), timezone_name=tz)
    assert confirmed["coverageStartDate"] == "2026-03-25"
    assert confirmed["lastSyncedAt"] == "2026-03-30T08:00:00+00:00"
    assert confirmed["days"]["2026-03-25"]["coverage"] == "partial"
    assert confirmed["days"]["2026-03-27"] == {"coverage": "covered", "eventCount": 1}
    assert confirmed["days"]["2026-03-29"] == {"coverage": "covered", "eventCount": 0}
    assert confirmed["days"]["2026-03-30"]["coverage"] == "partial"
    assert "2026-03-31" not in confirmed["days"]
    assert (
        confirmed["weeks"]["2026-03-23"]
        == confirmed["weeks"]["2026-03-30"]
        == "partial"
    )
    assert confirmed["summary"] == {
        "days": {"covered": 4, "partial": 2, "unverified": 0},
        "weeks": {"covered": 0, "partial": 2, "unverified": 0},
    }


def test_history_browsing_counts_legacy_events_in_local_calendar_periods(monkeypatch):
    monkeypatch.setattr(data_sync, "_now_utc", lambda: utc("2026-02-03T08:00:00"))
    Cache().activity.save(
        {
            make_event("february", date=datetime(2024, 2, 29, 12)),
            make_event("march", date=utc("2024-02-29T23:30:00")),
            make_event("new-year", date=utc("2025-12-31T23:30:00")),
        }
    )

    history = activity_history_status(date(2024, 2, 1), timezone_name="Europe/Warsaw")

    assert history["years"] == {
        "2024": {"coverage": "unverified", "eventCount": 2},
        "2025": {"coverage": "unverified", "eventCount": 0},
        "2026": {"coverage": "unverified", "eventCount": 1},
    }
    months = history["months"]
    assert months["2024-01"] == {"coverage": "unverified", "eventCount": 0}
    assert months["2024-02"] == {"coverage": "unverified", "eventCount": 1}
    assert months["2024-03"] == {"coverage": "unverified", "eventCount": 1}
    assert months["2025-12"] == {"coverage": "unverified", "eventCount": 0}
    assert months["2026-01"] == {"coverage": "unverified", "eventCount": 1}
    assert months["2026-02"] == {"coverage": "unverified", "eventCount": 0}
    assert "2026-03" not in months


def test_history_browsing_reports_coverage_for_periods_without_events(monkeypatch):
    monkeypatch.setattr(data_sync, "_now_utc", lambda: utc("2026-02-03T08:00:00"))
    cache = Cache()
    cache.activity.save(set())
    cache.activity_coverage.save(
        {
            "event_count": 0,
            "intervals": [(utc("2024-12-31T23:00:00"), utc("2026-02-01T00:00:00"))],
        }
    )

    history = activity_history_status(timezone_name="Europe/Warsaw")

    assert history["years"] == {
        "2025": {"coverage": "covered", "eventCount": 0},
        "2026": {"coverage": "partial", "eventCount": 0},
    }
    assert history["months"]["2026-01"] == {"coverage": "covered", "eventCount": 0}
    assert history["months"]["2026-02"] == {"coverage": "partial", "eventCount": 0}


def test_failed_page_preserves_last_success_and_retries_across_gap(monkeypatch):
    now = utc("2026-11-12T12:00:00")
    monkeypatch.setattr(data_sync, "_now_utc", lambda: now)
    legacy = make_event("legacy", date=datetime(2026, 11, 10, 10))
    Cache().activity.save({legacy})
    db = DatabaseActivity()
    pages = iter([{"results": [], "next_cursor": None}])
    requests = []

    def request_json(spec, **_kwargs):
        requests.append(spec.params)
        response = next(pages)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(db._api_client, "request_json", request_json)
    sync_recent_activity(db)
    successful = activity_history_status()

    # A project-specific refresh can cache a newer event without verifying the gap.
    scoped = make_event("scoped", date=datetime(2026, 11, 19, 10))
    merge_activity_cache({scoped})
    now = utc("2026-11-20T12:00:00")
    fetched = {"results": [event_payload("new", event_date="2026-11-18T12:00:00Z")]}
    pages = iter(
        [{**fetched, "next_cursor": "next"}, RuntimeError("network interrupted")]
    )
    with pytest.raises(RuntimeError, match="network interrupted"):
        sync_recent_activity(db)

    after_failure = activity_history_status()
    assert after_failure["lastSyncedAt"] == successful["lastSyncedAt"]
    assert after_failure["days"]["2026-11-18"] == {
        "coverage": "unverified",
        "eventCount": 0,
    }
    assert load_activity_cache() == {scoped, legacy}

    pages = iter([{**fetched, "next_cursor": None}])
    result = sync_recent_activity(db)
    assert {event.id for event in result.events} == {"legacy", "scoped", "new"}
    assert requests[-1]["date_from"] == "2026-11-11T00:00:00Z"
    assert requests[-1]["date_to"] == "2026-11-20T12:00:00Z"
    confirmed = activity_history_status()
    assert confirmed["days"]["2026-11-18"] == {"coverage": "covered", "eventCount": 1}
    assert confirmed["weeks"]["2026-11-09"] == "covered"
    assert confirmed["lastSyncedAt"] == now.isoformat()


def test_empty_sync_records_success_and_missing_activity_discards_coverage(monkeypatch):
    db = DatabaseActivity()
    monkeypatch.setattr(db, "fetch_activity_incremental", lambda *_args, **_kwargs: [])
    sync_recent_activity(db)

    history = activity_history_status()
    assert history["firstEventDate"] is None
    assert history["lastSyncedAt"] == "2026-11-22T12:00:00+00:00"
    assert history["days"]["2026-11-21"] == {"coverage": "covered", "eventCount": 0}

    Path(Cache().activity.path).unlink()
    assert activity_history_status()["lastSyncedAt"] is None


def test_demo_status_has_no_cached_user_activity(monkeypatch):
    Cache().activity.save({make_event("legacy", date=datetime(2026, 11, 20, 10))})
    db = DatabaseActivity()
    monkeypatch.setattr(db, "fetch_activity_incremental", lambda *_args, **_kwargs: [])
    sync_recent_activity(db)

    demo = activity_history_status(demo_mode=True)
    assert demo["firstEventDate"] is None
    assert demo["lastSyncedAt"] is None
    assert demo["days"]["2026-11-20"] == {"coverage": "unverified", "eventCount": 0}
    assert demo["years"] == {"2026": {"coverage": "unverified", "eventCount": 0}}
    assert demo["months"]["2026-11"] == {"coverage": "unverified", "eventCount": 0}
