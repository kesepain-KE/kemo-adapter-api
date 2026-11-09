from __future__ import annotations

import hmac
import secrets
import time
from uuid import uuid4

import yaml

from core.config import PrincipalConfig
from gateway.security import SecretVault, preview
from gateway.store import Store, dump, utc_now


class KeyPolicyError(ValueError):
    pass


class KeyService:
    def __init__(self, store: Store, vault: SecretVault):
        self.store, self.vault = store, vault

    def list(self):
        with self.store._lock:
            return [{"id": r["id"], "prefix": r["prefix"], "created_at": r["created_at"],
                     "can_copy": r["encrypted"] is not None,
                     **yaml.safe_load(r["body"])} for r in self.store._db.execute(
                         "SELECT * FROM downstream_keys ORDER BY created_at DESC")]

    @staticmethod
    def validate(policy):
        allowed = {"name", "note", "allowed_models", "allowed_ops", "rpm", "tpm", "budget", "currency", "enabled", "preview"}
        if set(policy) - allowed:
            raise ValueError("密钥策略包含未知字段")
        for field in ("rpm", "tpm"):
            if not isinstance(policy.get(field, 0), int) or policy.get(field, 0) < 0:
                raise ValueError(f"{field} 必须是非负整数")
        budget = policy.get("budget")
        if budget is not None and (not isinstance(budget, (int, float)) or budget < 0 or budget != budget or budget == float("inf")):
            raise ValueError("预算必须是非负有限数字")
        if policy.get("currency", "USD") not in {"USD", "CNY", "EUR", "JPY", "GBP"}:
            raise ValueError("预算币种无效")
        models = policy.get("allowed_models")
        if models is not None and (not isinstance(models, list) or any(not isinstance(m, str) for m in models)):
            raise ValueError("模型白名单必须为字符串数组或 null")
        if not set(policy.get("allowed_ops", ["llm", "embedding", "rerank"])) <= {"llm", "embedding", "rerank"}:
            raise ValueError("密钥操作范围无效")
        if not isinstance(policy.get("enabled", True), bool):
            raise ValueError("enabled 必须是布尔值")
        if not isinstance(policy.get("name", ""), str) or len(policy.get("name", "")) > 120:
            raise ValueError("密钥名称无效")

    def create(self, policy, custom_token=None):
        self.validate(policy)
        token = custom_token or "kg_" + secrets.token_urlsafe(32)
        if len(token) < 32 or len(set(token)) < 12:
            raise ValueError("自定义密钥至少 32 字符，且应包含足够的随机性")
        key_id = "key_" + uuid4().hex
        policy = {"enabled": True, "rpm": 0, "tpm": 0, "budget": None,
                  "currency": "USD", "allowed_models": None,
                  "allowed_ops": ["llm", "embedding", "rerank"], **policy, "preview": preview(token)}
        with self.store.transaction() as db:
            db.execute("""INSERT INTO downstream_keys(id,prefix,key_hash,body,created_at,encrypted)
                          VALUES(?,?,?,?,?,?)""", (
                key_id, token[:12], self.vault.fingerprint(token), dump(policy), utc_now(),
                self.vault.encrypt(token, key_id),
            ))
        return key_id, token

    def reveal(self, key_id):
        with self.store._lock:
            row = self.store._db.execute("SELECT encrypted FROM downstream_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            raise LookupError("密钥不存在")
        if row[0] is None:
            raise KeyPolicyError("此历史密钥只保存了哈希，无法还原；请创建新密钥")
        return self.vault.decrypt(row[0], key_id)

    def delete(self, key_id):
        with self.store.transaction() as db:
            if not db.execute("DELETE FROM downstream_keys WHERE id=?", (key_id,)).rowcount:
                raise LookupError("密钥不存在")

    def authenticate(self, token: str):
        # Bootstrap-only instances can start without creating master material.
        with self.store._lock:
            rows = self.store._db.execute("SELECT * FROM downstream_keys WHERE prefix=?", (token[:12],)).fetchall()
        if not rows:
            return None
        digest = self.vault.fingerprint(token)
        for row in rows:
            if hmac.compare_digest(digest, row["key_hash"]):
                policy = yaml.safe_load(row["body"])
                if not policy.get("enabled", True):
                    return None
                operations = policy.get("allowed_ops", ["llm", "embedding", "rerank"])
                scopes = {"asset:read", "asset:write"}
                if "llm" in operations:
                    scopes.add("llm:invoke")
                if "embedding" in operations:
                    scopes.add("embedding:invoke")
                if "rerank" in operations:
                    scopes.add("rerank:invoke")
                return PrincipalConfig("gateway", row["id"], frozenset(scopes), row["id"],
                                       frozenset(policy["allowed_models"]) if policy.get("allowed_models") is not None else None)
        return None

    def patch(self, key_id, patch):
        with self.store.transaction() as db:
            row = db.execute("SELECT body FROM downstream_keys WHERE id=?", (key_id,)).fetchone()
            if row is None:
                raise LookupError("密钥不存在")
            policy = {**yaml.safe_load(row[0]), **patch}
            self.validate(policy)
            db.execute("UPDATE downstream_keys SET body=? WHERE id=?", (dump(policy), key_id))

    def admit(self, context, model, deployments, max_attempts=1):
        if context.gateway_key_id is None:
            return
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM admission WHERE response_id=?", (context.response_id,)).fetchone():
                return
            row = db.execute("SELECT body FROM downstream_keys WHERE id=?", (context.gateway_key_id,)).fetchone()
            if row is None:  # Explicit bootstrap credentials, never persisted as downstream keys.
                return
            policy = yaml.safe_load(row[0])
            if not policy.get("enabled", True):
                raise KeyPolicyError("密钥已停用")
            ops = policy.get("allowed_ops", ["llm", "embedding", "rerank"])
            allowed = policy.get("allowed_models")
            if model.task not in ops or (allowed is not None and model.id not in allowed):
                raise KeyPolicyError("密钥不允许此模型或操作")
            now = time.time()
            minute = db.execute("""SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0)
                FROM admission WHERE key_id=? AND started>?""", (context.gateway_key_id, now - 60)).fetchone()
            if policy.get("rpm") and minute[0] >= policy["rpm"]:
                raise KeyPolicyError("密钥 RPM 已达上限")
            # Local estimates cannot guarantee a ceiling. Admission reserves the
            # model's declared hard bounds and only exact usage releases tokens.
            caps = model.declaration()
            bound = caps.limits.context_window
            if bound is None and caps.limits.max_input_tokens and caps.limits.max_output_tokens:
                bound = caps.limits.max_input_tokens + caps.limits.max_output_tokens
            reserve = 0
            if policy.get("tpm"):
                if bound is None:
                    raise KeyPolicyError("TPM 限额需要模型声明 token 硬上限")
                reserve = bound * max_attempts
                if minute[1] + reserve > policy["tpm"]:
                    raise KeyPolicyError("密钥 TPM 预留额度不足")
            reserved_cost = 0
            currency = policy.get("currency", "USD")
            if policy.get("budget") is not None:
                if bound is None:
                    raise KeyPolicyError("预算限额需要模型 token 硬上限")
                prices = [d.price for d in deployments]
                if any(p.currency != currency or p.input_per_million is None or p.output_per_million is None for p in prices):
                    raise KeyPolicyError("预算限额需要所有候选 Deployment 的同币种价格")
                reserved_cost = bound * max_attempts * max(max(p.input_per_million, p.output_per_million) for p in prices) / 1_000_000
                spent = db.execute("SELECT COALESCE(SUM(reserved_cost),0) FROM admission WHERE key_id=?",
                                   (context.gateway_key_id,)).fetchone()[0]
                if spent + reserved_cost > policy["budget"]:
                    raise KeyPolicyError("密钥预算不足")
            db.execute("INSERT INTO admission VALUES(?,?,?,?,?,?,0)", (
                context.gateway_key_id, context.response_id, now, reserve, reserved_cost, currency,
            ))

    def settle(self, context):
        with self.store.transaction() as db:
            totals = db.execute("""SELECT SUM(u.body IS NULL) AS missing FROM attempts a
                LEFT JOIN usage_evidence u USING(request_id,attempt_no) WHERE a.response_id=?""",
                (context.response_id,)).fetchone()
            usages = [yaml.safe_load(r[0]) for r in db.execute(
                "SELECT u.body FROM usage_evidence u JOIN attempts a USING(request_id,attempt_no) WHERE a.response_id=?", (context.response_id,))]
            known_tokens = bool(usages) and all(
                u.get("total_tokens") is not None and "total_tokens" in u["measurement"]["exact_fields"] for u in usages
            )
            costs = db.execute("""SELECT SUM(c.cost),MIN(c.complete),COUNT(*) FROM cost_records c
                JOIN attempts a USING(request_id,attempt_no) WHERE a.response_id=?""", (context.response_id,)).fetchone()
            row = db.execute("SELECT reserved_tokens,reserved_cost FROM admission WHERE response_id=?", (context.response_id,)).fetchone()
            if row:
                db.execute("UPDATE admission SET reserved_tokens=?,reserved_cost=?,finished=1 WHERE response_id=?", (
                    sum(u["total_tokens"] for u in usages) if known_tokens and not totals["missing"] else row[0],
                    costs[0] if costs[2] and costs[1] and not totals["missing"] else row[1], context.response_id,
                ))
