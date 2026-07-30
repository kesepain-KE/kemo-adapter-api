"""Short-lived Web control-plane authentication sessions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
import time


WEB_SESSION_TTL_SECONDS = 2 * 60 * 60
WEB_SESSION_COOKIE = "kemo_web_session"
WEB_PREAUTH_COOKIE = "kemo_web_preauth"
WEB_LOGIN_WINDOW_SECONDS = 15 * 60
WEB_LOGIN_MAX_FAILURES = 8
WEB_LOGIN_TRACKED_CLIENTS = 4096
WEB_MAX_ACTIVE_SESSIONS = 4096


@dataclass(frozen=True, slots=True)
class WebSession:
    token: str
    csrf_token: str
    stage: str
    expires_monotonic: float
    expires_at: str


class WebAuthService:
    def __init__(
        self,
        ttl_seconds: int = WEB_SESSION_TTL_SECONDS,
        *,
        persistence_path: Path | None = None,
        namespace: str = "default",
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.persistence_path = persistence_path
        self.namespace = namespace
        self._sessions: dict[str, WebSession] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._lock = Lock()
        self._load_persisted_sessions()

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, *, stage: str) -> WebSession:
        if stage not in {"password", "complete"}:
            raise ValueError("invalid Web session stage")
        now = datetime.now(timezone.utc)
        session = WebSession(
            token=f"web_{secrets.token_urlsafe(32)}",
            csrf_token=f"csrf_{secrets.token_urlsafe(32)}",
            stage=stage,
            expires_monotonic=time.monotonic() + self.ttl_seconds,
            expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
        )
        with self._lock:
            self._purge_expired_locked()
            self._sessions[self._session_key(session.token)] = session
            if len(self._sessions) > WEB_MAX_ACTIVE_SESSIONS:
                oldest = min(
                    self._sessions,
                    key=lambda token: self._sessions[token].expires_monotonic,
                )
                self._sessions.pop(oldest, None)
            self._persist_locked()
        return session

    def resolve(self, token: str, *, stage: str | None = None) -> WebSession | None:
        with self._lock:
            changed = self._purge_expired_locked()
            session = self._sessions.get(self._session_key(token))
            if changed:
                self._persist_locked()
            if session is None or (stage is not None and session.stage != stage):
                return None
            return session

    def revoke(self, token: str) -> None:
        with self._lock:
            removed = self._sessions.pop(self._session_key(token), None)
            if removed is not None:
                self._persist_locked()

    def login_retry_after(self, client_id: str) -> int:
        """Return seconds until another login attempt is allowed."""
        now = time.monotonic()
        with self._lock:
            failures = self._recent_failures_locked(client_id, now)
            if len(failures) < WEB_LOGIN_MAX_FAILURES:
                return 0
            return max(1, int(WEB_LOGIN_WINDOW_SECONDS - (now - failures[0])))

    def record_login_failure(self, client_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            failures = self._recent_failures_locked(client_id, now)
            failures.append(now)
            self._login_failures[client_id] = failures
            if len(self._login_failures) > WEB_LOGIN_TRACKED_CLIENTS:
                oldest = min(
                    self._login_failures,
                    key=lambda key: self._login_failures[key][-1],
                )
                self._login_failures.pop(oldest, None)

    def clear_login_failures(self, client_id: str) -> None:
        with self._lock:
            self._login_failures.pop(client_id, None)

    def _purge_expired_locked(self) -> bool:
        now = time.monotonic()
        expired = [token for token, session in self._sessions.items() if session.expires_monotonic <= now]
        for token in expired:
            self._sessions.pop(token, None)
        return bool(expired)

    def _load_persisted_sessions(self) -> None:
        path = self.persistence_path
        if path is None:
            return
        try:
            raw = path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                return
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("namespace") != self.namespace:
            return
        entries = value.get("sessions")
        if not isinstance(entries, list):
            return
        now_utc = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        for entry in entries[:WEB_MAX_ACTIVE_SESSIONS]:
            if not isinstance(entry, dict):
                continue
            token_hash = entry.get("token_hash")
            csrf_token = entry.get("csrf_token")
            stage = entry.get("stage")
            expires_at = entry.get("expires_at")
            if (
                not isinstance(token_hash, str)
                or len(token_hash) != 64
                or not isinstance(csrf_token, str)
                or not csrf_token.startswith("csrf_")
                or stage not in {"password", "complete"}
                or not isinstance(expires_at, str)
            ):
                continue
            try:
                expires = datetime.fromisoformat(expires_at)
                if expires.tzinfo is None:
                    continue
                remaining = (expires.astimezone(timezone.utc) - now_utc).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
            if remaining <= 0:
                continue
            self._sessions[token_hash] = WebSession(
                token="",
                csrf_token=csrf_token,
                stage=stage,
                expires_monotonic=now_monotonic + remaining,
                expires_at=expires_at,
            )

    def _persist_locked(self) -> None:
        path = self.persistence_path
        if path is None:
            return
        from core.restart_control import atomic_json

        atomic_json(
            path,
            {
                "version": 1,
                "namespace": self.namespace,
                "sessions": [
                    {
                        "token_hash": token_hash,
                        "csrf_token": session.csrf_token,
                        "stage": session.stage,
                        "expires_at": session.expires_at,
                    }
                    for token_hash, session in self._sessions.items()
                ],
            },
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _recent_failures_locked(self, client_id: str, now: float) -> list[float]:
        cutoff = now - WEB_LOGIN_WINDOW_SECONDS
        failures = [
            value for value in self._login_failures.get(client_id, ()) if value > cutoff
        ]
        if failures:
            self._login_failures[client_id] = failures
        else:
            self._login_failures.pop(client_id, None)
        return failures
