from __future__ import annotations

import json
import time
import yaml

from core.models import Usage, UsageMeasurement
from gateway.domain import Price
from gateway.store import Store, dump, utc_now
from gateway.token_counting import estimate_usage
from gateway.tracing import AttemptTrace, document


def normalize_usage(raw: dict | None, *, input_field: str, output_field: str,
                    cache_field: str | None = None, reasoning_field: str | None = None,
                    input_includes_cache: bool = True, output_includes_reasoning: bool = True) -> Usage:
    """Adapters explicitly identify inclusive/exclusive vendor field semantics."""
    if not raw:
        return Usage()
    def count(field):
        value = raw
        for key in (field or "").split("."):
            value = value.get(key) if isinstance(value, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    input_tokens, output_tokens = count(input_field), count(output_field)
    cached, reasoning = count(cache_field), count(reasoning_field)
    if not input_includes_cache and input_tokens is not None:
        input_tokens += cached or 0
    if not output_includes_reasoning and output_tokens is not None:
        output_tokens += reasoning or 0
    values = {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cached_input_tokens": cached, "reasoning_tokens": reasoning,
        "total_tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
    }
    fields = [k for k, v in values.items() if v is not None]
    complete = input_tokens is not None and output_tokens is not None
    return Usage(**values, measurement=UsageMeasurement(
        mode="provider" if fields else "unknown", exact=complete,
        exact_fields=fields,
    ))


def cost(usage: Usage, price: Price) -> tuple[float | None, bool]:
    if usage.input_tokens is None or usage.output_tokens is None:
        return None, False
    if price.input_per_million is None or price.output_per_million is None:
        return None, False
    cached = usage.cached_input_tokens or 0
    if cached and price.cached_input_per_million is None:
        return None, False
    result = (
        max(0, usage.input_tokens - cached) * price.input_per_million
        + cached * (price.cached_input_per_million or 0)
        + usage.output_tokens * price.output_per_million
    ) / 1_000_000
    return result, usage.measurement.exact


class Metering:
    def __init__(self, store: Store, *, log_detail_max_bytes=4 * 1024 * 1024, log_detail_mode="full"):
        self.store = store
        self.log_detail_max_bytes = log_detail_max_bytes
        self.log_detail_mode = log_detail_mode
        self._active = {}

    def detail(self, request_id, attempt_no):
        trace = self._active.get((request_id, attempt_no))
        if trace is not None:
            return trace.payload(self.log_detail_max_bytes) if trace.capture_details else None
        return self.store.attempt_detail(request_id, attempt_no)

    def start(self, context, no, deployment, upstream, *, request=None, method=None, credential=None, task="llm"):
        trace = AttemptTrace(
            document(request), tenant_id=context.tenant_id, operation=method,
            capture_details=self.log_detail_mode != "off",
        )
        with self.store.transaction() as db:
            self.store.prune_details(db)
            key = db.execute("SELECT body FROM downstream_keys WHERE id=?", (context.gateway_key_id,)).fetchone()
            key_name = yaml.safe_load(key[0]).get("name") if key else None
            db.execute("""INSERT INTO attempts(
                request_id,attempt_no,response_id,deployment_id,upstream_id,model,
                key_id,started_at,status,price,credential_id,upstream_name,upstream_model,
                task,log_type,subject_id,key_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                context.response_id, no, context.response_id, deployment.id, upstream.id,
                deployment.public_model, context.gateway_key_id, utc_now(), "running",
                dump(deployment.price.model_dump()),
                credential.id if credential else None, getattr(upstream, "name", "") or upstream.id, deployment.upstream_model,
                task, context.subject_id if context.subject_id in {"model-test", "capability-probe"} else "request",
                context.subject_id, key_name,
            ))
        self._active[(context.response_id, no)] = trace
        return trace

    def finish(self, context, no, started, deployment, usage, *, error=None, cancelled=False,
               upstream=None, task="llm"):
        trace = started
        try:
            latency_ms = (time.monotonic() - trace.started) * 1000
            tokenizer_name = None
            if not error and not cancelled and upstream is not None:
                usage, tokenizer_name = estimate_usage(
                    usage, trace.request, document(trace.response), model=deployment.upstream_model,
                    setting=upstream.tokenizer, task=task, system_prompt=context.gateway_system_prompt,
                )
            if trace.response is None and error is not None:
                trace.response = {"error": error.model_dump(mode="json")}
            amount, complete = cost(usage, deployment.price)
            source = "exact" if usage.measurement.exact else (
                "estimate" if usage.measurement.estimated_fields else
                "partial" if usage.measurement.exact_fields else "unknown"
            )
            body = None
            if self.log_detail_mode == "full" or (self.log_detail_mode == "errors" and (error or cancelled)):
                body = json.dumps(trace.payload(self.log_detail_max_bytes), ensure_ascii=False)
            with self.store.transaction() as db:
                db.execute("""UPDATE attempts SET status=?,latency_ms=?,error_code=?,first_token_ms=?,tokenizer=?
                    WHERE request_id=? AND attempt_no=?""", (
                    "cancelled" if cancelled else "failed" if error else "completed",
                    latency_ms,
                    error.code if error else None, trace.first_token_ms, tokenizer_name, context.response_id, no,
                ))
                db.execute("INSERT INTO usage_evidence VALUES(?,?,?,?)", (
                    context.response_id, no, source, dump(usage.model_dump(mode="json")),
                ))
                db.execute("INSERT INTO cost_records VALUES(?,?,?,?,?)", (
                    context.response_id, no, amount, deployment.price.currency, int(complete),
                ))
                if body is not None:
                    db.execute("INSERT INTO attempt_details VALUES(?,?,?)", (context.response_id, no, body))
        finally:
            self._active.pop((context.response_id, no), None)
