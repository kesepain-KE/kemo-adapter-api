"""Short-lived Web control-plane authentication sessions."""

from __future__ import annotations

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
    def __init__(self, ttl_seconds: int = WEB_SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, WebSession] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._lock = Lock()

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
            self._sessions[session.token] = session
            if len(self._sessions) > WEB_MAX_ACTIVE_SESSIONS:
                oldest = min(
                    self._sessions,
                    key=lambda token: self._sessions[token].expires_monotonic,
                )
                self._sessions.pop(oldest, None)
        return session

    def resolve(self, token: str, *, stage: str | None = None) -> WebSession | None:
        with self._lock:
            self._purge_expired_locked()
            session = self._sessions.get(token)
            if session is None or (stage is not None and session.stage != stage):
                return None
            return session

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

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

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [token for token, session in self._sessions.items() if session.expires_monotonic <= now]
        for token in expired:
            self._sessions.pop(token, None)

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
