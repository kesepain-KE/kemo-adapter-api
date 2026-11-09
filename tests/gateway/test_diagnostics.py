from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from gateway.diagnostics import request_activity, usage_overview
from gateway.store import Store, dump
from tests.support.admin import ADMIN_HEADERS, CALLER_HEADERS, admin_settings
from tests.support.gateway import add_upstream, instance, mutate


def record(store, identity, at, *, log_type="request", status="completed", usage=None,
           provider="old-provider", key="old-key", cost=None, currency="USD",
           latency=100, first_token=20, source="exact"):
    with store.transaction() as db:
        db.execute("""INSERT INTO attempts(request_id,attempt_no,response_id,deployment_id,upstream_id,
            upstream_name,model,key_id,key_name,started_at,status,price,log_type,latency_ms,first_token_ms)
            VALUES(?,1,?,'old-route',?,'Retired provider','old-model',?,?,?,?,'{}',?,?,?)""",
                   (identity, identity, provider, key, "Retired key" if key else None,
                    at.isoformat(), status, log_type, latency, first_token))
        if usage is not None:
            db.execute("INSERT INTO usage_evidence VALUES(?,1,?,?)", (identity, source, dump(usage)))
        if cost is not None:
            db.execute("INSERT INTO cost_records VALUES(?,1,?,?,1)", (identity, cost, currency))


def test_overview_aggregates_full_history_known_cache_and_separate_currencies(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    with instance(tmp_path, monkeypatch) as (client, app):
        store = app.state.registry.store
        for index in range(120):
            record(store, f"row-{index}", now, usage={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cached_input_tokens": 2,
            }, cost=.001, source="estimate" if index == 119 else "exact")
        for identity, status in (("failed", "failed"), ("running", "running")):
            record(store, identity, now, status=status, provider="other", key=None,
                   latency=50 if status == "failed" else None, first_token=None)
        record(store, "partial", now, provider="other", key=None, usage={"input_tokens": 100},
               cost=.5, currency="CNY", latency=300, first_token=None)
        response = client.get("/admin/api/diagnostics/usage?period=all")
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers["cache-control"]
        result = response.json()
        metrics = result["summary"]
        assert metrics["calls"] == 123 and metrics["finished"] == 122 and metrics["successful"] == 121
        assert metrics["success_rate"] == pytest.approx(121 / 122)
        assert (metrics["input_tokens"], metrics["output_tokens"], metrics["total_tokens"]) == (1300, 600, 1800)
        assert metrics["cached_tokens"] == 240 and metrics["cache_hit_rate"] == pytest.approx(.2)
        assert metrics["first_token_ms"] == 20 and metrics["first_token_samples"] == 120
        assert metrics["latency_ms"] == pytest.approx(12350 / 122)
        assert metrics["costs"] == pytest.approx({"USD": .12, "CNY": .5})
        assert metrics["unknown_cost_calls"] == 2 and metrics["estimated_calls"] == 1
        assert metrics["upstream_count"] == 2 and metrics["deployment_count"] == 1
        providers = result["rankings"]["provider"]
        assert [(row["id"], row["calls"]) for row in providers] == [("old-provider", 120), ("other", 3)]
        assert providers[0]["name"] == "Retired provider"
        assert result["rankings"]["key"][0]["name"] == "Retired key"
        unknown = result["rankings"]["key"][1]
        assert unknown["id"] == "/unattributed" and unknown["cached_tokens"] is None
        assert unknown["cache_hit_rate"] is None and unknown["first_token_ms"] is None
        assert len(result["rankings"]["model"]) == 1
        assert client.get("/admin/api/diagnostics/usage?period=90d").status_code == 422


def test_overview_timezone_windows_exclude_probes_and_future_rows(tmp_path):
    store = Store(tmp_path)
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    try:
        for identity, at in (
            ("today", datetime(2026, 11, 7, 16, tzinfo=timezone.utc)),
            ("before-midnight", datetime(2026, 11, 7, 15, 59, 59, tzinfo=timezone.utc)),
            ("seven", now - timedelta(days=7)),
            ("before-seven", now - timedelta(days=7, seconds=1)),
            ("thirty", now - timedelta(days=30)),
            ("old", now - timedelta(days=31)),
            ("future", now + timedelta(seconds=1)),
        ):
            record(store, identity, at, log_type=None if identity == "old" else "request")
        for log_type in ("model-test", "capability-probe"):
            record(store, log_type, now, log_type=log_type)
        for period, expected in (("today", 1), ("7d", 3), ("30d", 5), ("all", 6)):
            overview = usage_overview(store, period, "Asia/Shanghai", now=now)
            assert overview["summary"]["calls"] == expected
            assert overview["summary"]["total_tokens"] is None
        today = usage_overview(store, "today", "Asia/Shanghai", now=now)
        assert today["start"] == "2026-11-08T00:00:00+08:00"
        assert usage_overview(store, "today", "UTC", now=now)["summary"]["calls"] == 0
    finally:
        store.close()


def test_overview_empty_and_admin_access(tmp_path):
    app = create_app(admin_settings(), live_config_root=tmp_path)
    with TestClient(app) as client:
        path = "/admin/api/diagnostics/usage"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=CALLER_HEADERS).status_code == 403
        response = client.get(path, headers=ADMIN_HEADERS)
        assert response.status_code == 200
        metrics = response.json()["summary"]
        assert metrics["calls"] == metrics["total_tokens"] == metrics["cached_tokens"] == 0
        assert metrics["success_rate"] is metrics["cache_hit_rate"] is metrics["first_token_ms"] is None
        assert metrics["costs"] == {} and metrics["unknown_cost_calls"] == 0
        assert metrics["upstream_count"] == metrics["deployment_count"] == 0


def test_overview_rolling_24_hours_and_distinct_routes_follow_the_selected_window(tmp_path, monkeypatch):
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    with instance(tmp_path, monkeypatch) as (client, app):
        store = app.state.registry.store
        for identity, at, provider in (
            ("today", datetime(2026, 11, 7, 16, tzinfo=timezone.utc), "current"),
            ("boundary", now - timedelta(hours=24), "previous"),
            ("before", now - timedelta(hours=24, microseconds=1), "older"),
        ):
            record(store, identity, at, provider=provider,
                   usage={"total_tokens": 10}, cost=.25)
            with store.transaction() as db:
                db.execute("UPDATE attempts SET deployment_id=? WHERE request_id=?", (identity, identity))
        record(store, "repeat", now, provider="current", status="running")
        with store.transaction() as db:
            db.execute("UPDATE attempts SET deployment_id='today' WHERE request_id='repeat'")
        for log_type in ("model-test", "capability-probe"):
            record(store, log_type, now, provider="probe-only", log_type=log_type)
        record(store, "future", now + timedelta(seconds=1), provider="future")
        today = usage_overview(store, "today", "Asia/Shanghai", now=now)["summary"]
        assert (today["calls"], today["upstream_count"], today["deployment_count"]) == (2, 1, 1)
        rolling = usage_overview(store, "24h", "Asia/Shanghai", now=now)
        assert rolling["start"] == (now - timedelta(hours=24)).isoformat()
        summary = rolling["summary"]
        assert (summary["calls"], summary["upstream_count"], summary["deployment_count"]) == (3, 2, 2)
        assert summary["success_rate"] == 1 and summary["total_tokens"] == 20
        assert summary["costs"] == {"USD": .5}
        for period in ("7d", "30d", "all"):
            summary = usage_overview(store, period, "Asia/Shanghai", now=now)["summary"]
            assert (summary["calls"], summary["upstream_count"], summary["deployment_count"]) == (4, 3, 3)
        monkeypatch.setattr("web.backend.router.usage_overview",
                            lambda store, period, zone: usage_overview(store, period, zone, now=now))
        response = client.get("/admin/api/diagnostics/usage?period=24h")
        assert response.status_code == 200 and response.json()["summary"] == rolling["summary"]


def test_legacy_rankings_use_current_names_without_overriding_recorded_names(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        assert mutate(client, "/upstreams/local", {"name": "Readable provider"}, method="PATCH").status_code == 200
        key = mutate(client, "/keys", {"name": "Readable key"}, key=True).json()
        store = app.state.registry.store
        record(store, "legacy", datetime.now(timezone.utc), provider="local", key=key["id"])
        with store.transaction() as db:
            db.execute("UPDATE attempts SET upstream_name=NULL,key_name=NULL WHERE request_id='legacy'")
        result = client.get("/admin/api/diagnostics/usage?period=all").json()["rankings"]
        assert result["provider"][0]["name"] == "Readable provider"
        assert result["key"][0]["name"] == "Readable key"
        with store.transaction() as db:
            db.execute("UPDATE attempts SET upstream_name='Recorded provider',key_name='Recorded key'")
        result = client.get("/admin/api/diagnostics/usage?period=all").json()["rankings"]
        assert result["provider"][0]["name"] == "Recorded provider"
        assert result["key"][0]["name"] == "Recorded key"


def test_key_rankings_do_not_use_credentials_or_internal_ids_as_display_names(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        key = mutate(client, "/keys", {"name": ""}, key=True).json()
        record(app.state.registry.store, "unnamed", datetime.now(timezone.utc), key=key["id"])
        record(app.state.registry.store, "deleted", datetime.now(timezone.utc), key="key_deleted")
        with app.state.registry.store.transaction() as db:
            db.execute("UPDATE attempts SET key_name=NULL")
        response = client.get("/admin/api/diagnostics/usage?period=all")
        assert response.status_code == 200
        assert key["token"] not in response.text
        assert key["token"][:12] not in response.text
        rows = response.json()["rankings"]["key"]
        assert {row["id"] for row in rows} == {key["id"], "key_deleted"}
        assert all(row["name"] in (None, "") for row in rows)


def test_activity_uses_full_daily_history_and_preserves_unknowns(tmp_path):
    store = Store(tmp_path)
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    try:
        for index in range(120):
            record(store, f"today-{index}", now, usage={
                "input_tokens": 100 if index == 119 else 10, "output_tokens": 5,
                "total_tokens": 105 if index == 119 else 15,
                "cached_input_tokens": 60 if index == 119 else 2,
            })
        record(store, "midnight", datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        record(store, "outside", datetime(2026, 10, 31, 15, 59, 59, tzinfo=timezone.utc))
        record(store, "previous-day", datetime(2026, 11, 7, 15, 59, 59, tzinfo=timezone.utc),
               status="failed", log_type=None, latency=None, first_token=None)
        record(store, "future", now + timedelta(seconds=1))
        record(store, "probe", now, log_type="capability-probe")
        record(store, "test", now, log_type="model-test")
        result = request_activity(store, "month", "Asia/Shanghai", now=now)
        assert (result["start"], result["end"], len(result["days"])) == ("2026-11-01", "2026-11-30", 30)
        assert sum(day["calls"] for day in result["days"]) == 122
        today = result["days"][7]
        assert today["date"] == "2026-11-08" and today["future"] is False
        assert today["calls"] == 120 and today["success_rate"] == 1
        assert (today["input_tokens"], today["output_tokens"], today["cached_tokens"]) == (1290, 600, 298)
        assert today["cache_hit_rate"] == pytest.approx(298 / 1290)
        assert today["first_token_ms"] == 20
        unknown = result["days"][6]
        assert unknown["calls"] == 1 and unknown["success_rate"] == 0
        assert unknown["input_tokens"] is unknown["cached_tokens"] is unknown["first_token_ms"] is None
        empty = result["days"][8]
        assert empty["future"] is True and empty["calls"] == empty["total_tokens"] == 0
        assert empty["success_rate"] is empty["cache_hit_rate"] is None
        assert request_activity(store, "week", "Asia/Shanghai", now=now)["start"] == "2026-11-02"
    finally:
        store.close()


@pytest.mark.parametrize(("period", "day", "start", "end", "length"), [
    ("month", date(2024, 2, 15), "2024-02-01", "2024-02-29", 29),
    ("week", date(2026, 1, 1), "2025-12-29", "2026-01-04", 7),
    ("month", date(2025, 12, 15), "2025-12-01", "2025-12-31", 31),
])
def test_activity_calendar_boundaries(tmp_path, period, day, start, end, length):
    store = Store(tmp_path)
    try:
        result = request_activity(store, period, "UTC", day=day)
        assert (result["start"], result["end"], len(result["days"])) == (start, end, length)
        assert all(day["calls"] == 0 for day in result["days"])
    finally:
        store.close()


def test_activity_day_boundaries_follow_daylight_saving_time(tmp_path):
    store = Store(tmp_path)
    try:
        for index, at in enumerate(("2026-11-01T03:59:59", "2026-11-01T04:00:00",
                                    "2026-11-01T05:30:00", "2026-11-01T06:30:00",
                                    "2026-11-02T04:59:59", "2026-11-02T05:00:00")):
            record(store, f"dst-{index}", datetime.fromisoformat(at).replace(tzinfo=timezone.utc))
        result = request_activity(store, "week", "America/New_York", day=date(2026, 11, 1),
                                now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        assert result["start"] == "2026-10-26" and result["end"] == "2026-11-01"
        assert result["days"][-2]["calls"] == 1 and result["days"][-1]["calls"] == 4
        assert sum(day["calls"] for day in result["days"]) == 5
    finally:
        store.close()


def test_activity_requires_admin_and_validates_ranges_granularity_and_dates(tmp_path):
    app = create_app(admin_settings(), live_config_root=tmp_path)
    with TestClient(app) as client:
        path = "/admin/api/diagnostics/activity"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=CALLER_HEADERS).status_code == 403
        response = client.get(path + "?period=month&date=2024-02-08", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        assert len(response.json()["days"]) == 29
        response = client.get(path + "?period=26w", headers=ADMIN_HEADERS)
        assert response.status_code == 200 and len(response.json()["days"]) == 182
        response = client.get(path + "?period=week&granularity=hour", headers=ADMIN_HEADERS)
        assert response.status_code == 200 and len(response.json()["hours"]) == 168
        assert client.get(path + "?period=year", headers=ADMIN_HEADERS).status_code == 422
        assert client.get(path + "?granularity=minute", headers=ADMIN_HEADERS).status_code == 422
        assert client.get(path + "?date=2026-02-30", headers=ADMIN_HEADERS).status_code == 422


def test_hourly_activity_uses_local_clock_and_preserves_weighted_metrics(tmp_path):
    store = Store(tmp_path)
    now = datetime(2026, 11, 8, 10, 35, tzinfo=timezone.utc)
    try:
        for index, at in enumerate((
            datetime(2026, 11, 7, 15, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 11, 7, 16, tzinfo=timezone.utc),
            now.replace(minute=0), now,
        )):
            record(store, f"hour-{index}", at, status="failed" if index == 3 else "completed",
                   usage={"input_tokens": 10 if index < 3 else 100, "cached_input_tokens": 2 if index < 3 else 60})
        record(store, "unknown-hour", now.replace(hour=9), first_token=None, latency=None)
        for log_type in ("model-test", "capability-probe"):
            record(store, log_type, now, log_type=log_type)
        record(store, "future", now + timedelta(seconds=1))
        result = request_activity(store, "week", "Asia/Shanghai", granularity="hour", now=now)
        assert (result["start"], result["end"], len(result["hours"])) == ("2026-11-02", "2026-11-08", 168)
        hours = {(row["date"], row["hour"]): row for row in result["hours"]}
        assert sum(row["calls"] for row in hours.values()) == 5
        assert hours["2026-11-07", 23]["calls"] == hours["2026-11-08", 0]["calls"] == 1
        current = hours["2026-11-08", 18]
        assert current["calls"] == 2 and current["success_rate"] == .5 and current["future"] is False
        assert current["input_tokens"] == 110 and current["cached_tokens"] == 62
        assert current["cache_hit_rate"] == pytest.approx(62 / 110)
        unknown = hours["2026-11-08", 17]
        assert unknown["calls"] == 1 and unknown["input_tokens"] is unknown["first_token_ms"] is None
        assert hours["2026-11-08", 19]["future"] is True and hours["2026-11-08", 19]["calls"] == 0
        assert hours["2026-11-08", 16]["future"] is False
        daily = request_activity(store, "26w", "Asia/Shanghai", now=now)
        assert len(daily["days"]) == 182 and daily["start"] == "2026-05-11"
        assert sum(day["calls"] for day in daily["days"]) == 5
    finally:
        store.close()


def test_hourly_activity_merges_repeated_daylight_saving_hour(tmp_path):
    store = Store(tmp_path)
    try:
        for index, at in enumerate(("2026-11-01T05:30:00", "2026-11-01T06:30:00")):
            record(store, f"fold-{index}", datetime.fromisoformat(at).replace(tzinfo=timezone.utc))
        result = request_activity(store, "week", "America/New_York", granularity="hour",
                                  day=date(2026, 11, 1), now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        repeated = next(hour for hour in result["hours"] if hour["date"] == "2026-11-01" and hour["hour"] == 1)
        assert len(result["hours"]) == 168 and repeated["calls"] == 2
        assert sum(hour["calls"] for hour in result["hours"]) == 2
    finally:
        store.close()
