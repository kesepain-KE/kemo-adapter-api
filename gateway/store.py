from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import yaml

from gateway.domain import Catalog
from gateway.model_names import public_model_name
from gateway.auth import legacy_scheme


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(value: Any) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=True)


def revision(catalog: Catalog) -> str:
    document = catalog.model_dump(mode="json")
    for name in ("upstreams", "models", "deployments", "extensions"):
        document[name] = sorted(document[name], key=lambda item: (item.get("kind", ""), item["id"]))
    return hashlib.sha256(dump(document).encode()).hexdigest()


class RevisionConflict(ValueError):
    pass


def read_catalog(db) -> Catalog:
    result = {
        name: [yaml.safe_load(row[0]) for row in db.execute(f"SELECT body FROM {name} ORDER BY id")]
        for name in ("upstreams", "models", "deployments")
    }
    for identity, body in db.execute("SELECT id,body FROM options"):
        result[identity] = yaml.safe_load(body)
    return Catalog.model_validate(result)


class Store:
    def __init__(self, root: Path, *, detail_retention_hours=24):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "gateway.sqlite3"
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.detail_retention_hours = detail_retention_hours
        self._next_details_cleanup = 0
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS upstreams(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS options(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS downstream_keys(
                id TEXT PRIMARY KEY, prefix TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS key_prefix ON downstream_keys(prefix);
            CREATE TABLE IF NOT EXISTS attempts(
                request_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, response_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL, upstream_id TEXT NOT NULL, model TEXT NOT NULL,
                key_id TEXT, started_at TEXT NOT NULL, status TEXT NOT NULL,
                latency_ms REAL, error_code TEXT, price TEXT NOT NULL,
                PRIMARY KEY(request_id,attempt_no));
            CREATE TABLE IF NOT EXISTS usage_evidence(
                request_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                source TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(request_id,attempt_no));
            CREATE TABLE IF NOT EXISTS cost_records(
                request_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                cost REAL, currency TEXT NOT NULL, complete INTEGER NOT NULL,
                PRIMARY KEY(request_id,attempt_no));
            CREATE TABLE IF NOT EXISTS admission(
                key_id TEXT NOT NULL, response_id TEXT PRIMARY KEY,
                started REAL NOT NULL, reserved_tokens INTEGER NOT NULL,
                reserved_cost REAL NOT NULL, currency TEXT NOT NULL,
                finished INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS admission_key_time ON admission(key_id,started);
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempt_details(
                request_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(request_id,attempt_no));
            CREATE INDEX IF NOT EXISTS attempts_time ON attempts(started_at);
        """)
        # Existing installations retain their keys and attempt history.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(downstream_keys)")}
        if "encrypted" not in columns:
            self._db.execute("ALTER TABLE downstream_keys ADD COLUMN encrypted TEXT")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(attempts)")}
        for name, definition in {
            "credential_id": "TEXT", "upstream_name": "TEXT", "upstream_model": "TEXT",
            "task": "TEXT", "log_type": "TEXT", "subject_id": "TEXT", "key_name": "TEXT",
            "first_token_ms": "REAL", "tokenizer": "TEXT",
        }.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE attempts ADD COLUMN {name} {definition}")
        self._db.commit()
        self._migrate_model_names()

    def _migrate_auth_schemes(self):
        changes = []
        with self.transaction() as db:
            for row in db.execute("SELECT id,body FROM upstreams").fetchall():
                value = yaml.safe_load(row["body"])
                if "auth_type" not in value:
                    continue
                old = value.pop("auth_type")
                value.setdefault("auth_scheme", legacy_scheme(value["adapter_id"], old))
                db.execute("UPDATE upstreams SET body=? WHERE id=?", (dump(value), row["id"]))
                changes.append({"upstream": row["id"], "from": old, "to": value["auth_scheme"]})
            if changes:
                db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)",
                           (utc_now(), "system", "auth-scheme-conversion", dump(changes)))
        for change in changes:
            logging.getLogger(__name__).warning("鉴权字段转换: %s", change)

    def _migrate_model_names(self):
        """Rename legacy aliases and their live references in one transaction."""
        with self.transaction() as db:
            models = [yaml.safe_load(row[0]) for row in db.execute("SELECT body FROM models ORDER BY id")]
            upstreams = {row["id"]: yaml.safe_load(row["body"]) for row in db.execute("SELECT id,body FROM upstreams")}
            deployments = [yaml.safe_load(row[0]) for row in db.execute("SELECT body FROM deployments ORDER BY id")]
            used = {model["id"] for model in models}
            renamed = {}
            for model in models:
                old = model["id"]
                if not re.search(r"[/\\\s]", old):
                    continue
                deployment = next((d for d in deployments if d["public_model"] == old), None)
                upstream = upstreams[deployment["upstream_id"]] if deployment else {}
                name = public_model_name(upstream.get("name", "gateway"),
                                         deployment["upstream_model"] if deployment else old,
                                         upstream.get("template_id"))
                if name in used:
                    name = name[:190] + "-" + hashlib.sha256(old.encode()).hexdigest()[:8]
                if name in used:
                    raise ValueError("公开模型迁移发生名称冲突")
                renamed[old] = name
                used.add(name)
                model["id"] = name
                db.execute("UPDATE models SET id=?,body=? WHERE id=?", (name, dump(model), old))
            if renamed:
                for deployment in deployments:
                    if deployment["public_model"] in renamed:
                        deployment["public_model"] = renamed[deployment["public_model"]]
                        db.execute("UPDATE deployments SET body=? WHERE id=?", (dump(deployment), deployment["id"]))
                self._rename_key_models(db, renamed)
                db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)",
                           (utc_now(), "system", "rename-public-models", dump(renamed)))

    @staticmethod
    def _rename_key_models(db, renamed, removed=()):
        for row in db.execute("SELECT id,body FROM downstream_keys").fetchall():
            policy = yaml.safe_load(row["body"])
            allowed = policy.get("allowed_models")
            if allowed is not None and any(name in renamed or name in removed for name in allowed):
                policy["allowed_models"] = list(dict.fromkeys(
                    renamed.get(name, name) for name in allowed if name not in removed))
                db.execute("UPDATE downstream_keys SET body=? WHERE id=?", (dump(policy), row["id"]))

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def catalog(self) -> Catalog:
        with self._lock:
            return read_catalog(self._db)

    def save(self, catalog: Catalog, expected: str, actor: str, *, model_renames=None):
        with self.transaction() as db:
            if revision(self.catalog()) != expected:
                raise RevisionConflict("目录已更新，请刷新后重试")
            removed_models = {row[0] for row in db.execute("SELECT id FROM models")} - {
                model.id for model in catalog.models} - set(model_renames or {})
            for name in ("upstreams", "models", "deployments"):
                old = {r[0] for r in db.execute(f"SELECT id FROM {name}")}
                for item in getattr(catalog, name):
                    db.execute(f"INSERT INTO {name}(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                               (item.id, dump(item.model_dump(mode="json"))))
                    old.discard(item.id)
                db.executemany(f"DELETE FROM {name} WHERE id=?", ((i,) for i in old))
            for name in ("settings", "extensions"):
                value = catalog.model_dump(mode="json")[name]
                db.execute("INSERT INTO options VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (name, dump(value)))
            if model_renames or removed_models:
                self._rename_key_models(db, model_renames or {}, removed_models)
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)", (utc_now(), actor, "publish", revision(catalog)))

    def audit(self, actor: str, action: str, target: str):
        with self.transaction() as db:
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)", (utc_now(), actor, action, target))

    def prune_details(self, db):
        if time.monotonic() < self._next_details_cleanup:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.detail_retention_hours)).isoformat()
        db.execute("""DELETE FROM attempt_details WHERE (request_id,attempt_no) IN (
            SELECT request_id,attempt_no FROM attempts WHERE started_at<?)""", (cutoff,))
        self._next_details_cleanup = time.monotonic() + 300

    def cleanup_expired_details(self):
        with self.transaction() as db:
            self.prune_details(db)

    def attempts(self, limit=100, offset=0, model: str | None = None, *,
                 start=None, end=None, status=None, task=None, log_type=None):
        clauses, params = [], []
        for column, operator, value in (
            ("model", "=", model), ("started_at", ">=", start), ("started_at", "<=", end),
            ("status", "=", status), ("task", "=", task), ("log_type", "=", log_type),
        ):
            if value:
                clauses.append(f"a.{column}{operator}?")
                params.append(value)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"""SELECT a.*,u.source,u.body AS usage,c.cost,c.currency,c.complete
            FROM attempts a LEFT JOIN usage_evidence u USING(request_id,attempt_no)
            LEFT JOIN cost_records c USING(request_id,attempt_no) {where}
            ORDER BY a.started_at DESC,a.attempt_no DESC LIMIT ? OFFSET ?"""
        with self._lock:
            rows = self._db.execute(query, (*params, limit, offset)).fetchall()
            return [{**dict(r), "price": yaml.safe_load(r["price"]), "usage": yaml.safe_load(r["usage"]) if r["usage"] else None} for r in rows]

    def attempt_detail(self, request_id, attempt_no):
        with self._lock:
            row = self._db.execute(
                "SELECT body FROM attempt_details WHERE request_id=? AND attempt_no=?",
                (request_id, attempt_no),
            ).fetchone()
            exists = self._db.execute(
                "SELECT 1 FROM attempts WHERE request_id=? AND attempt_no=?", (request_id, attempt_no),
            ).fetchone()
        if exists is None:
            raise LookupError("调用记录不存在")
        if row is None:
            return None
        body = zlib.decompress(row[0]) if isinstance(row[0], bytes) else row[0]
        return yaml.safe_load(body)

    def close(self):
        with self._lock:
            self._db.close()
