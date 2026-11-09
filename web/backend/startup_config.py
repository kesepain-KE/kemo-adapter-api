"""Owner editing of startup YAML, with secret preservation and atomic writes."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

from core.secret_preview import mask_secret
from core.yaml_config import MAX_CONFIG_BYTES, dump_yaml, parse_yaml, startup_values
from web.backend.gateway_config import GROUPS, SECRET_FIELDS


EDITABLE = {name for _, fields in GROUPS for name, _, _ in fields} | {
    "LOG_LEVEL", "WEB_ACCESS_LOG", "WEB_OPEN_BROWSER", "RESTART_DRAIN_TIMEOUT", "RESTART_STARTUP_TIMEOUT",
}
SECRETS = {name for name, _ in SECRET_FIELDS}
lock = threading.Lock()


class StartupConflict(ValueError):
    pass


def read_document(path):
    content = path.read_bytes() if path.exists() else b""
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError("启动配置文件过大")
    return content, parse_yaml(content.decode("utf-8-sig")) if content.strip() else {}


def config_view(root, environment_overrides=()):
    with lock:
        content, document = read_document(root / "gateway.yaml")
    visible = {name: value for name, value in document.items() if name in EDITABLE}
    # An invalid legacy URL can carry credentials; never return them to a form.
    hidden = []
    if any(char in str(visible.get("GATEWAY_BASE_URL", "")) for char in "@?#"):
        visible.pop("GATEWAY_BASE_URL")
        hidden.append("GATEWAY_BASE_URL")
    return {
        "expected_revision": hashlib.sha256(content).hexdigest(),
        "yaml": dump_yaml(visible), "source": "gateway.yaml", "hidden_fields": hidden,
        "secrets": [{"name": name, "label": label, "configured": bool(document.get(name)),
                     "preview": mask_secret(document.get(name))} for name, label in SECRET_FIELDS],
        "environment_overrides": sorted(set(environment_overrides) & (EDITABLE | SECRETS)),
    }


def validate_startup():
    # Keep validation free of api.server imports and application/DB initialization.
    from core.config import Settings
    from start_web import _env_bool, _startup_options
    from zoneinfo import ZoneInfo

    _startup_options()
    settings = Settings.from_env()
    ZoneInfo(settings.statistics_timezone)
    _env_bool("WEB_OPEN_BROWSER", False)
    if settings.base_url:
        parsed = urlsplit(settings.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Invalid gateway base URL")
        parsed.port  # Reject malformed or out-of-range URL ports.
    for value in (settings.model_execution_timeout_seconds, settings.sse_heartbeat_seconds):
        if not math.isfinite(value):
            raise ValueError("Timeouts must be finite")
    for name in ("RESTART_DRAIN_TIMEOUT", "RESTART_STARTUP_TIMEOUT"):
        if not 1 <= float(os.getenv(name, "60")) <= 3600:
            raise ValueError("Restart timeout is out of range")


def save_config(root: Path, expected_revision, text, secrets):
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("启动配置文件过大")
    values = parse_yaml(text)
    if set(values) - EDITABLE or set(secrets) - SECRETS:
        raise ValueError("配置包含不可编辑字段；敏感项请使用专用输入框")
    if any(not isinstance(value, str) or len(value) > 8192 for value in secrets.values()):
        raise ValueError("敏感配置格式无效")
    path = root / "gateway.yaml"
    with lock:
        content, current = read_document(path)
        if hashlib.sha256(content).hexdigest() != expected_revision:
            raise StartupConflict("启动配置已更新，请重新加载后再编辑")
        hidden_url = any(char in str(current.get("GATEWAY_BASE_URL", "")) for char in "@?#")
        candidate = {name: value for name, value in current.items()
                     if name not in EDITABLE or name == "GATEWAY_BASE_URL" and hidden_url}
        candidate.update(values)
        candidate.update(secrets)
        payload = dump_yaml(candidate)
        if len(payload.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("启动配置文件过大")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".yaml",
                                             prefix=".gateway-", dir=root, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            env = {name: value for name, value in os.environ.items() if name not in EDITABLE | SECRETS}
            env.update(startup_values(temporary))
            checked = subprocess.run(
                [sys.executable, "-c", "from web.backend.startup_config import validate_startup; validate_startup()"],
                cwd=Path(__file__).resolve().parents[2], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if checked.returncode:
                raise ValueError("启动配置校验失败，请检查监听、认证、时区及数值范围")
            if read_document(path)[0] != content:
                raise StartupConflict("启动配置已更新，请重新加载后再编辑")
            if path.exists():
                os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
