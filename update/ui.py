"""面向小白用户的纯终端交互组件。"""

from __future__ import annotations


def confirm(prompt: str, default: bool = False) -> bool:
    """用数字或常见 yes/no 输入完成二次确认。"""

    default_label = "是" if default else "否"
    while True:
        try:
            raw = input(
                f"{prompt}（输入 1=是，2=否，直接回车={default_label}）: "
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

