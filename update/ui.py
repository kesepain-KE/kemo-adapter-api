"""面向小白用户的终端交互组件。

交互层只负责显示、读取选择和二次确认，不直接执行 Git 或文件操作。这样
命令行参数、交互菜单和测试可以共享同一套安全边界。
"""

from __future__ import annotations

import os
import shutil
import sys


def _use_color() -> bool:
    return not os.environ.get("NO_COLOR") and bool(sys.stdout.isatty())


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def title(text: str) -> None:
    width = max(58, min(88, shutil.get_terminal_size((80, 24)).columns))
    print("\n" + "=" * width)
    print(text.center(width))
    print("=" * width)


def section(text: str) -> None:
    print(f"\n--- {text} ---")


def note(text: str) -> None:
    print(f"[提示] {text}")


def warning(text: str) -> None:
    print(f"[警告] {text}")


def error(text: str) -> None:
    print(f"[错误] {text}")

def confirm(prompt: str, default: bool = False) -> bool:
    """用数字或常见 yes/no 输入完成二次确认。"""

    default_label = "是" if default else "否"
    while True:
        try:
            raw = input(
                f"{prompt}（1=是，2=否，直接回车={default_label}）: "
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        normalized = raw.strip().lower()
        if not normalized:
            return default
        if normalized in {"1", "y", "yes", "是"}:
            return True
        if normalized in {"0", "2", "n", "no", "否"}:
            return False
        print("[提示] 请输入 1 或 2。")


def read_choice(
    prompt: str,
    *,
    valid: set[str],
    default: str | None = None,
) -> str:
    """读取菜单选择；无输入终端绝不会隐式执行写操作。"""

    while True:
        default_hint = f"，直接回车={default}" if default is not None else ""
        try:
            raw = input(f"{prompt}{default_hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "0" if "0" in valid else (default or next(iter(valid)))
        choice = raw or default
        if choice in valid:
            return choice
        print(f"[提示] 请输入：{' / '.join(sorted(valid))}")


def pause() -> None:
    try:
        input("\n按回车键返回主菜单...")
    except (EOFError, KeyboardInterrupt):
        print()


def read_text(prompt: str, *, default: str = "") -> str:
    """读取可选文本；EOF 和 Ctrl+C 返回默认值。"""

    try:
        answer = input(f"{prompt}{f'（默认：{default}）' if default else ''}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    answer = answer.strip()
    return answer or default

