"""Aggregate persisted upstream attempts without loading request/response bodies."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

from gateway.currency import display_cost


def new_metrics():
    return {
        "calls": 0, "successful": 0, "finished": 0,
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0,
        "token_samples": 0, "input_samples": 0, "output_samples": 0, "cache_samples": 0,
        "cache_input_tokens": 0, "cache_hits_for_rate": 0, "latency_sum": 0, "latency_samples": 0,
        "first_token_sum": 0, "first_token_samples": 0,
        "costs": {}, "unknown_cost_calls": 0, "estimated_calls": 0,
    }


def accumulate(metrics, row, usage):
    metrics["calls"] += 1
    metrics["successful"] += row["status"] == "completed"
    metrics["finished"] += row["status"] != "running"
    for name, samples in (("input_tokens", "input_samples"), ("output_tokens", "output_samples"),
                          ("total_tokens", "token_samples"), ("cached_input_tokens", "cache_samples")):
        value = usage.get(name)
        if value is not None:
            metrics["cached_tokens" if name == "cached_input_tokens" else name] += value
            metrics[samples] += 1
    if usage.get("cached_input_tokens") is not None and usage.get("input_tokens") is not None:
        metrics["cache_input_tokens"] += usage["input_tokens"]
        metrics["cache_hits_for_rate"] += usage["cached_input_tokens"]
    for field, prefix in (("latency_ms", "latency"), ("first_token_ms", "first_token")):
        if row[field] is not None:
            metrics[prefix + "_sum"] += row[field]
            metrics[prefix + "_samples"] += 1
    if row["cost"] is None:
        metrics["unknown_cost_calls"] += 1
    else:
        currency = row["currency"]
        metrics["costs"][currency] = metrics["costs"].get(currency, 0) + row["cost"]
    metrics["estimated_calls"] += row["source"] == "estimate"


def finish(metrics, display_currency=None):
    result = {**metrics}
    result["success_rate"] = metrics["successful"] / metrics["finished"] if metrics["finished"] else None
    result["cache_hit_rate"] = metrics["cache_hits_for_rate"] / metrics["cache_input_tokens"] if metrics["cache_input_tokens"] else None
    for prefix in ("latency", "first_token"):
        result[prefix + "_ms"] = metrics[prefix + "_sum"] / metrics[prefix + "_samples"] if metrics[prefix + "_samples"] else None
        del result[prefix + "_sum"]
    for field, samples in (("input_tokens", "input_samples"), ("output_tokens", "output_samples"),
                          ("total_tokens", "token_samples"), ("cached_tokens", "cache_samples")):
        if metrics["calls"] and not metrics[samples]:
            result[field] = None
    if display_currency is not None:
        result["display_cost"] = display_cost(
            metrics["costs"], display_currency,
            approximate=bool(metrics["unknown_cost_calls"] or metrics["estimated_calls"]),
        )
    return result


def usage_overview(store, period, timezone_name, *, now=None, display_currency="USD"):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(timezone_name))
    if period == "today":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "24h":
        start = now - timedelta(hours=24)
    else:
        start = now - timedelta(days=int(period[:-1])) if period != "all" else None
    clauses = ["(a.log_type='request' OR a.log_type IS NULL)", "a.started_at<=?"]
    params = [now.astimezone(timezone.utc).isoformat()]
    if start is not None:
        clauses.append("a.started_at>=?")
        params.append(start.astimezone(timezone.utc).isoformat())
    query = f"""SELECT a.deployment_id,a.upstream_id,a.upstream_name,a.model,a.key_id,a.key_name,a.status,
        a.latency_ms,a.first_token_ms,u.body AS usage,u.source,c.cost,c.currency
        FROM attempts a LEFT JOIN usage_evidence u USING(request_id,attempt_no)
        LEFT JOIN cost_records c USING(request_id,attempt_no) WHERE {' AND '.join(clauses)}
        ORDER BY a.started_at"""
    total = new_metrics()
    groups = {"provider": {}, "model": {}, "key": {}}
    deployments = set()
    with store._lock:
        provider_names = {row["id"]: yaml.safe_load(row["body"])["name"]
                          for row in store._db.execute("SELECT id,body FROM upstreams")}
        key_names = {row["id"]: yaml.safe_load(row["body"]).get("name")
                     for row in store._db.execute("SELECT id,body FROM downstream_keys")}
        for row in store._db.execute(query, params):
            usage = yaml.safe_load(row["usage"]) if row["usage"] else {}
            accumulate(total, row, usage)
            deployments.add(row["deployment_id"])
            for dimension, identity, name in (
                ("provider", row["upstream_id"], row["upstream_name"] or provider_names.get(row["upstream_id"]) or row["upstream_id"]),
                ("model", row["model"], row["model"]),
                ("key", row["key_id"] or "/unattributed", row["key_name"] or key_names.get(row["key_id"])),
            ):
                group = groups[dimension].setdefault(identity, {"id": identity, "name": name, **new_metrics()})
                group["name"] = name
                accumulate(group, row, usage)
    return {
        "period": period, "timezone": timezone_name,
        "start": start.isoformat() if start is not None else None, "end": now.isoformat(),
        "summary": {**finish(total, display_currency), "upstream_count": len(groups["provider"]), "deployment_count": len(deployments)},
        "rankings": {dimension: sorted((finish(group, display_currency) for group in values.values()),
                                      key=lambda item: (-item["calls"], item["id"]))
                     for dimension, values in groups.items()},
    }


def request_activity(store, period, timezone_name, *, granularity="day", day=None, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    today = local_now.date()
    day = day or today
    if period == "month":
        start = day.replace(day=1)
        length = monthrange(start.year, start.month)[1]
    else:
        start = day - timedelta(days=day.weekday())
        length = 182 if period == "26w" else 7
        if period == "26w":
            start -= timedelta(weeks=25)
    end = start + timedelta(days=length)
    dates = [(start + timedelta(days=index)).isoformat() for index in range(length)]
    buckets = {(date, hour): new_metrics()
               for date in dates for hour in (range(24) if granularity == "hour" else (None,))}
    query = """SELECT a.started_at,a.status,a.latency_ms,a.first_token_ms,
        u.body AS usage,u.source,c.cost,c.currency FROM attempts a
        LEFT JOIN usage_evidence u USING(request_id,attempt_no)
        LEFT JOIN cost_records c USING(request_id,attempt_no)
        WHERE (a.log_type='request' OR a.log_type IS NULL)
        AND a.started_at>=? AND a.started_at<? AND a.started_at<=?
        ORDER BY a.started_at"""
    params = (
        datetime.combine(start, time.min, zone).astimezone(timezone.utc).isoformat(),
        datetime.combine(end, time.min, zone).astimezone(timezone.utc).isoformat(),
        now.astimezone(timezone.utc).isoformat(),
    )
    with store._lock:
        for row in store._db.execute(query, params):
            at = datetime.fromisoformat(row["started_at"]).astimezone(zone)
            bucket = (at.date().isoformat(), at.hour if granularity == "hour" else None)
            usage = yaml.safe_load(row["usage"]) if row["usage"] else {}
            accumulate(buckets[bucket], row, usage)
    return {
        "period": period, "granularity": granularity, "timezone": timezone_name, "today": today.isoformat(),
        "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
        # Hourly charts group by local clock hour; a repeated DST hour shares one cell.
        "hours" if granularity == "hour" else "days": [
            {"date": date, **({"hour": hour} if hour is not None else {}),
             "future": date > today.isoformat() or (
                 date == today.isoformat() and hour is not None and hour > local_now.hour),
             **finish(metrics)}
            for (date, hour), metrics in buckets.items()
        ],
    }
