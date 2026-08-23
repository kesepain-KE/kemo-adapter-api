"""Kemo 网关更新器的唯一真实应用入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from update import backup, checker, git, integrity, ui, version
from update.constants import ROOT
from update.transaction import perform_update


def _configure_utf8_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _print_menu(project_root: Path) -> None:
    local = version.read_local(project_root)
    print("\n" + "=" * 58)
    print("                 Kemo 网关更新工具")
    print("=" * 58)
    print(f"当前版本：{local.version}    Kemo 协议：{local.protocol_version}")
    print("\n  1. 检查并安装更新（推荐）")
    print("  2. 只检查更新，不修改文件")
    print("  3. 查看或恢复更新前备份")
    print("  4. 修复网关源码（高级操作）")
    print("  0. 退出")


def check_command(project_root: Path) -> int:
    code, _, _ = checker.check(project_root)
    return code


def apply(project_root: Path, *, yes: bool = False) -> int:
    code, local, remote = checker.check(project_root)
    if code != 0 or local is None or remote is None:
        return code or 1

    source_state = integrity.check_source_state(project_root)
    if not source_state.ok:
        print(f"[ERROR] {source_state.message}：")
        for file_name in source_state.files:
            print(f"      - {file_name}")
        print("[ERROR] 请运行 python update.py 并选择“修复网关源码”。")
        return 5

    state = git.get_sync_state(project_root)
    if state.relation == "up_to_date":
        print("[KEMO] 没有需要应用的新提交；未修改任何文件。")
        return 0
    if state.relation == "ahead":
        print("[ERROR] 本地领先远端，普通更新已拒绝；不会丢弃本地提交。")
        return 4
    if state.relation == "diverged":
        print("[ERROR] 本地与远端分叉，普通更新已拒绝。请先人工确认历史。")
        return 4
    if state.relation != "behind":
        print("[ERROR] 无法确认安全快进条件。")
        return 1
    if checker.reject_protected_remote_changes(project_root):
        return 4

    diff = git.get_remote_diff(project_root)
    target_commit = git.get_fetch_commit(project_root)
    if not target_commit:
        print("[ERROR] FETCH_HEAD 无有效提交，已中止。")
        return 1
    if not yes and not ui.confirm("是否执行安全更新？", default=False):
        print("[KEMO] 已取消。")
        return 0
    return perform_update(
        project_root,
        local,
        remote,
        is_repair=False,
        yes=yes,
        diff=diff,
        target_commit=target_commit,
    )


def repair(project_root: Path, *, yes: bool = False) -> int:
    code, local, remote = checker.check(project_root)
    if code != 0 or local is None or remote is None:
        return code or 1
    if checker.reject_protected_remote_changes(project_root):
        return 4

    state = git.get_sync_state(project_root)
    target_commit = git.get_fetch_commit(project_root)
    if state.relation == "unknown" or not target_commit:
        print("[ERROR] 无法确认修复目标，已中止。")
        return 1

    print("[WARN] 修复会重新对齐 Git 已跟踪源码；私有数据不会由远端覆盖。")
    print("[WARN] 本地源码修改会保存在冷备份中，但不会重新覆盖修复后的源码。")
    if state.ahead:
        print(f"[WARN] 本地有 {state.ahead} 个远端不存在的提交；修复前会创建 Git 恢复引用。")
    if not yes and not ui.confirm(
        f"确认将已跟踪源码修复到 {target_commit[:12]}？",
        default=False,
    ):
        print("[KEMO] 已取消。")
        return 0
    return perform_update(
        project_root,
        local,
        remote,
        is_repair=True,
        yes=yes,
        diff=git.get_remote_diff(project_root),
        target_commit=target_commit,
    )


def status(project_root: Path) -> int:
    code, _, _ = checker.check(project_root)
    if code != 0:
        return code
    state = git.get_sync_state(project_root)
    print(f"[KEMO] Git 状态: {state.relation} (ahead={state.ahead}, behind={state.behind})")
    print(f"[KEMO] 分支: {git.get_current_branch(project_root)}")
    print(f"[KEMO] 提交: {git.get_current_commit(project_root)[:12]}")
    source_state = integrity.check_source_state(project_root)
    print(f"[KEMO] 源码完整性: {'正常' if source_state.ok else source_state.message}")
    return 0 if source_state.ok else 5


def list_backups(project_root: Path) -> int:
    items = backup.list_backups(project_root)
    if not items:
        print("[KEMO] 没有可用的备份。")
        return 0
    print(f"[KEMO] 可用备份 ({len(items)} 个):")
    for backup_id in items:
        print(f"      .backup/{backup_id}/")
    print("\n[KEMO] 普通用户请运行 python update.py，然后选择“查看或恢复备份”。")
    return 0


def restore_backup(project_root: Path, backup_id: str) -> int:
    print(f"[KEMO] 准备从 .backup/{backup_id}/ 恢复同名源码文件。")
    print("[WARN] 私有配置、Provider 密钥、统计数据和开发目录不会被覆盖。")
    if not ui.confirm("确定要恢复吗？", default=False):
        print("[KEMO] 已取消恢复。")
        return 0
    ok, message = backup.restore(project_root, backup_id)
    print(f"[KEMO] {message}")
    if not ok:
        return 3
    source_state = integrity.check_source_state(project_root)
    if not source_state.ok:
        print(f"[ERROR] 恢复后的源码仍不完整: {source_state.message}")
        return 5
    print("[KEMO] 请重启网关使恢复生效。")
    return 0


def _interactive_backups(project_root: Path) -> int:
    items = backup.list_backups(project_root)
    print("\n[KEMO] 更新前备份")
    if not items:
        print("[KEMO] 当前没有可恢复的备份。")
        return 0
    print(f"[KEMO] 找到 {len(items)} 份备份：")
    for index, backup_id in enumerate(items, start=1):
        label = "（最新）" if index == 1 else ""
        print(f"  {index}. {backup_id} {label}")
    print("  0. 返回主菜单")
    valid = {str(index) for index in range(len(items) + 1)}
    choice = ui.read_choice("请选择要恢复的备份", valid=valid)
    if choice == "0":
        return 0
    return restore_backup(project_root, items[int(choice) - 1])


def interactive_menu(project_root: Path) -> int:
    while True:
        _print_menu(project_root)
        choice = ui.read_choice(
            "请选择操作",
            valid={"0", "1", "2", "3", "4"},
            default="1",
        )
        if choice == "0":
            print("[KEMO] 已退出，未修改任何文件。")
            return 0
        if choice == "1":
            return apply(project_root, yes=False)
        if choice == "2":
            result = check_command(project_root)
            ui.pause()
            if result != 0:
                print("[提示] 检查没有完成，可以稍后重试。")
            continue
        if choice == "3":
            result = _interactive_backups(project_root)
            ui.pause()
            if result != 0:
                print("[提示] 备份恢复没有完成，请检查上面的错误说明。")
            continue

        print("\n[WARN] 修复会重新对齐 Git 已跟踪的网关源码。")
        print("[WARN] 只有源码损坏或普通更新明确提示无法更新时才使用。")
        if not ui.confirm("是否进入源码修复流程？", default=False):
            print("[KEMO] 已取消修复。")
            continue
        return repair(project_root, yes=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kemo 网关安全更新系统；不带参数时进入交互菜单"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="只检查远端状态")
    actions.add_argument("--apply", action="store_true", help="执行安全快进更新")
    actions.add_argument("--repair", action="store_true", help="显式修复 Git 已跟踪源码")
    actions.add_argument("--status", action="store_true", help="显示版本与 Git 状态")
    actions.add_argument("--list-backups", action="store_true", help="列出冷备份")
    actions.add_argument(
        "--restore-backup",
        type=str,
        metavar="TIME",
        help="从冷备份恢复同名源码文件，参数为时间戳或 latest",
    )
    parser.add_argument("--yes", action="store_true", help="跳过 --apply 或 --repair 的交互确认")
    return parser


def main(argv: list[str] | None = None, *, project_root: Path = ROOT) -> int:
    _configure_utf8_console()
    parser = build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)
    if not effective_argv:
        return interactive_menu(project_root)
    if args.yes and not (args.apply or args.repair):
        parser.error("--yes 只能与 --apply 或 --repair 一起使用")
    if args.check:
        return check_command(project_root)
    if args.apply:
        return apply(project_root, yes=args.yes)
    if args.repair:
        return repair(project_root, yes=args.yes)
    if args.status:
        return status(project_root)
    if args.list_backups:
        return list_backups(project_root)
    if args.restore_backup:
        return restore_backup(project_root, args.restore_backup)
    return check_command(project_root)

