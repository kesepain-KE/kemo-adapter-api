"""Kemo 网关与 Web 管理端的生产启动入口。

环境变量在进程启动时一次性读取：进程环境优先，项目根目录 ``.env`` 只补充缺失值。
运行时配置仍由 api/core/providers 下的受控 JSON 文件负责，不在这里热加载环境变量。
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
from threading import Timer
from typing import Any
import webbrowser

from dotenv import load_dotenv
import uvicorn

from core.restart_control import (
    RestartPaths,
    clear_pid_metadata,
    process_exists,
    read_json,
    write_pid_metadata,
)


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
FRONTEND_DIST = PROJECT_ROOT / "web" / "frontend" / "dist"
LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})

import io
import logging
import re

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8")


class _RedactTokenQueryFilter(logging.Filter):
    _token_query = re.compile(r"([?&]token=)[^&\s]*", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._token_query.sub(r"\1<redacted>", value) if isinstance(value, str) else value
                for value in record.args
            )
        return True


def _install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _RedactTokenQueryFilter) for item in logger.filters):
        logger.addFilter(_RedactTokenQueryFilter())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")


def _startup_options() -> dict[str, Any]:
    # 延迟导入，确保项目 .env 在 Settings 读取前已经加载。
    from core.config import Settings

    settings = Settings.from_env()
    if not settings.host.strip():
        raise ValueError("HOST 不能为空")
    if not 1 <= settings.port <= 65535:
        raise ValueError("PORT 必须在 1 到 65535 之间")
    if bool(settings.web_username.strip()) != bool(settings.web_password.strip()):
        raise ValueError("WEB_USERNAME 与 WEB_PASSWORD 必须同时配置")
    status_token = settings.status_token.strip()
    if status_token and (
        status_token == settings.web_token.strip()
        or status_token in settings.api_keys
    ):
        raise ValueError("STATUS_TOKEN 必须独立于 Web Token 和模型调用密钥")

    log_level = os.getenv("LOG_LEVEL", "info").strip().lower()
    if log_level not in LOG_LEVELS:
        raise ValueError(f"LOG_LEVEL 必须是: {', '.join(sorted(LOG_LEVELS))}")

    return {
        "host": settings.host,
        "port": settings.port,
        "log_level": log_level,
        "access_log": _env_bool("WEB_ACCESS_LOG", True),
    }


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _port_has_listener(host: str, port: int) -> bool:
    """只读探测目标监听地址，阻止 wildcard/specific address 双实例并存。"""
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    connect_host = connect_host.strip("[]")
    try:
        with socket.create_connection((connect_host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _startup_conflict(paths: RestartPaths, *, host: str, port: int) -> str | None:
    """返回启动冲突类型；陈旧 PID 文件不会阻止新实例。"""
    metadata = read_json(paths.pid) or {}
    try:
        pid = int(metadata.get("pid", -1))
    except (TypeError, ValueError):
        pid = -1
    metadata_root = metadata.get("project_root")
    if (
        pid != os.getpid()
        and process_exists(pid)
        and isinstance(metadata_root, str)
        and metadata_root
        and Path(metadata_root).resolve() == PROJECT_ROOT.resolve()
    ):
        return "active_instance"
    if _port_has_listener(host, port):
        return "port_in_use"
    return None


def main() -> int:
    # 保证从任意工作目录执行 ``python path/to/start_web.py`` 都能导入项目包。
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)
    environment_override_names = list(os.environ)
    load_dotenv(ENV_FILE, override=False)

    index_file = FRONTEND_DIST / "index.html"
    if not index_file.is_file():
        print(
            "[ERROR] Web 前端尚未构建，请先在 web/frontend 执行 pnpm run build。",
            file=sys.stderr,
        )
        return 2

    try:
        options = _startup_options()
        open_browser = _env_bool("WEB_OPEN_BROWSER", False)
    except (KeyError, TypeError, ValueError):
        # 不打印原始配置或异常正文，避免 JSON 配置错误时意外回显密钥。
        print("[ERROR] 启动环境变量无效，请检查 .env.example。", file=sys.stderr)
        return 2

    paths = RestartPaths(PROJECT_ROOT)
    conflict = _startup_conflict(
        paths,
        host=str(options["host"]),
        port=int(options["port"]),
    )
    if conflict is not None:
        message = (
            "检测到本项目已有网关实例正在运行，请先关闭旧实例或使用 restart.py。"
            if conflict == "active_instance"
            else "目标 HOST/PORT 已有进程监听，请先关闭占用进程或修改端口。"
        )
        print(f"[ERROR] {message}", file=sys.stderr)
        return 4

    url = _browser_url(str(options["host"]), int(options["port"]))
    print(f"[KEMO] Web 管理端: {url}")
    print("[KEMO] 环境变量仅在启动时读取，修改后必须重启。")
    if open_browser:
        browser_timer = Timer(0.8, webbrowser.open, args=(url,))
        browser_timer.daemon = True
        browser_timer.start()

    from api.server import app

    config = uvicorn.Config(
        app,
        host=options["host"],
        port=options["port"],
        log_level=options["log_level"],
        access_log=options["access_log"],
        workers=1,
    )
    _install_access_log_redaction()
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    app.state.restart_service.configure_server(server)
    instance_id = app.state.runtime_state.instance_id
    write_pid_metadata(
        paths,
        pid=os.getpid(),
        instance_id=instance_id,
        host=str(options["host"]),
        port=int(options["port"]),
        environment_override_names=environment_override_names,
    )
    try:
        server.run()
        return 0 if server.started else 3
    except KeyboardInterrupt:
        print("\n[KEMO] 网关已关闭。")
        return 0
    finally:
        clear_pid_metadata(paths, instance_id)


if __name__ == "__main__":
    raise SystemExit(main())
