"""Short-lived Web control-plane authentication sessions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
import time


WEB_SESSION_TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True, slots=True)
class WebSession:
    token: str
    stage: str
    expires_monotonic: float
    expires_at: str


class WebAuthService:
    def __init__(self, ttl_seconds: int = WEB_SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, WebSession] = {}
        self._lock = Lock()

    def issue(self, *, stage: str) -> WebSession:
        if stage not in {"password", "complete"}:
            raise ValueError("invalid Web session stage")
        now = datetime.now(timezone.utc)
        session = WebSession(
            token=f"web_{secrets.token_urlsafe(32)}",
            stage=stage,
            expires_monotonic=time.monotonic() + self.ttl_seconds,
            expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
        )
        with self._lock:
            self._purge_expired_locked()
            self._sessions[session.token] = session
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

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [token for token, session in self._sessions.items() if session.expires_monotonic <= now]
        for token in expired:
            self._sessions.pop(token, None)
