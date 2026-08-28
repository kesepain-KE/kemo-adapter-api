"""更新器共享工具：终端交互、命令执行、脱敏、版本和原子文件操作。

这个模块只提供无业务含义的基础能力。更新板块、Git 状态和事务流程分别
位于其它模块，避免根入口再次变成一个难以维护的“大脚本”。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]


def git_control_dir(project_root: Path) -> Path | None:
    """返回 Git 的控制目录，兼容普通仓库和 worktree。

    普通仓库的 ``.git`` 是目录；Git worktree 或某些部署工具会把
    ``.git`` 写成包含 ``gitdir: ...`` 的文件。更新器的锁和维护标记
    必须放在这个控制目录中，不能让临时文件进入工作树或 stash。
    """

    root = project_root.resolve()
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    try:
        first_line = dot_git.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, UnicodeError, IndexError):
        return None
    prefix = "gitdir:"
    if not first_line.casefold().startswith(prefix):
        return None
    raw_path = first_line[len(prefix) :].strip()
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


class UpdateError(RuntimeError):
    """可预期的更新失败；消息可以直接展示给用户。"""


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "cookie",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session_secret",
        "token",
        "credential",
        "credentials",
    }
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_ -]?(?:key|token)|access[_ -]?token|refresh[_ -]?token|"
    r"device[_ -]?token|authorization|cookie|password|private[_ -]?key|"
    r"client[_ -]?secret|secret|token)\b\s*(?:=|:|：)\s*)[^\s,;]+"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
_PRIVATE_KEY_RE = re.compile(
    r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_URL_RE = re.compile(
    r"(?i)\b(?:https?|ssh|git\+ssh|git\+https?)://[^\s'\"<>]+"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(\b(?:https?|ssh|git\+ssh|git\+https?)://)"
    r"[^/?#\s@]+(?::[^/?#\s@]*)?@"
)
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|cookie|password|private[_-]?key|client[_-]?secret|"
    r"session[_-]?secret|secret|token|credential)s?\s*=)[^&#\s]*"
)


def _is_sensitive_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(
        ("_token", "_secret", "_password", "_credential", "_credentials", "_key")
    )


def _redact_url(value: str) -> str:
    def fallback(raw: str) -> str:
        """即使 URL 语法损坏，也不能把 userinfo 或 query 密钥原样返回。"""

        safe = _URL_USERINFO_RE.sub(r"\1***@", raw)
        return _URL_QUERY_SECRET_RE.sub(r"\1***", safe)

    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback(value)
    if not parsed.scheme or not parsed.netloc:
        return fallback(value)
    try:
        # 不读取 ``parsed.port``：非法端口会抛 ValueError，且不能因此退回
        # 原始字符串，否则 URL 中的密码会泄露到终端日志。
        raw_netloc = parsed.netloc
        hostport = raw_netloc.rsplit("@", 1)[-1]
        netloc = f"***@{hostport}" if "@" in raw_netloc else hostport
        query = urlencode(
            [
                (key, "***" if _is_sensitive_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return fallback(value)


def redact_text(value: object) -> str:
    """清理命令、Git 和网络错误中的常见密钥形态。"""

    text = str(value)
    text = _URL_RE.sub(lambda match: _redact_url(match.group(0)), text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _ASSIGNMENT_RE.sub(r"\1***", text)
    text = _SECRET_VALUE_RE.sub("***", text)
    text = _PRIVATE_KEY_RE.sub("***", text)
    return text


def redact_json(value: object, *, key: str | None = None) -> object:
    """复制一个 JSON 兼容对象并遮罩敏感字段。"""

    if key is not None and _is_sensitive_key(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(item_key): redact_json(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, tuple):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _redact_command_arg(value: object) -> str:
    return redact_text(value)


def format_command(command: Iterable[object]) -> str:
    return " ".join(_redact_command_arg(item) for item in command)


def green(text: str) -> str:
    return f"\033[0;32m{text}\033[0m"


def yellow(text: str) -> str:
    return f"\033[1;33m{text}\033[0m"


def red(text: str) -> str:
    return f"\033[0;31m{text}\033[0m"


def is_interactive() -> bool:
    """当前是否具备可安全询问用户的终端。"""

    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def ask_yes_no(
    prompt: str,
    *,
    default: bool,
    assume_yes: bool = False,
) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    if answer in {"y", "yes", "1", "是"}:
        return True
    if answer in {"n", "no", "0", "2", "否"}:
        return False
    print("[提示] 请输入 y/n（或 1/2）。")
    return ask_yes_no(prompt, default=default, assume_yes=assume_yes)


def ask_choice(
    prompt: str,
    choices: dict[str, str],
    *,
    default: str,
    assume_yes: bool = False,
) -> str:
    """读取有默认值的菜单选项，EOF/中断永远回到默认值。"""

    if assume_yes or not is_interactive():
        return default
    print(prompt)
    for key, label in choices.items():
        mark = "（默认）" if key == default else ""
        print(f"  {key}. {label}{mark}")
    while True:
        try:
            answer = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not answer:
            return default
        if answer in choices:
            return answer
        print("[提示] 可选：" + " / ".join(choices))


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    capture: bool = False,
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """统一执行外部命令，保证 UTF-8 边界和敏感参数脱敏。"""

    print("+ " + format_command(command))
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    kwargs: dict[str, object] = {
        "cwd": str(cwd or ROOT),
        "check": check,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": environment,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(command, **kwargs)  # type: ignore[arg-type]


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def require_commands(names: Iterable[str]) -> None:
    missing = [name for name in names if not command_exists(name)]
    if missing:
        raise UpdateError("缺少必需命令：" + ", ".join(missing))


def read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"无法读取 JSON 文件 {path}: {redact_text(exc)}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"{path} 不是 JSON 对象")
    return value


def write_json_atomic(path: Path, value: dict) -> None:
    """用同目录临时文件原子替换 JSON，避免留下半写入版本文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def parse_version(value: object) -> tuple[int, ...]:
    text = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        raise UpdateError(f"无效版本号：{value!r}")
    return tuple(int(part) for part in text.split("."))


def compare_versions(left: object, right: object) -> int:
    """比较两个版本；返回 -1、0、1。"""

    left_parts = parse_version(left)
    right_parts = parse_version(right)
    size = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (size - len(left_parts))
    right_parts += (0,) * (size - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)


def tree_digest(path: Path) -> str:
    """计算文件或目录的稳定 SHA-256，用于预览和幂等同步。"""

    if not path.exists():
        return ""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
