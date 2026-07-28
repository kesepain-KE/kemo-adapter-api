"""Kemo 网关交互式更新入口。

普通用户只需运行 ``python update.py``，然后按屏幕提示输入数字。
命令行参数仅保留给自动化部署和高级维护，不是日常更新的必需操作。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from update import backup, deps, frontend, git, version


PROJECT_ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _confirm(prompt: str, default: bool = False) -> bool:
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
        if not raw:
            return default
        if raw.strip().lower() in ("1", "y", "yes", "是"):
            return True
        if raw.strip().lower() in ("0", "2", "n", "no", "否"):
            return False
        print("[提示] 请输入 1 或 2。")


def _read_choice(
    prompt: str,
    *,
    valid: set[str],
    default: str | None = None,
) -> str:
    """Read one menu choice without ever turning EOF into a mutation."""
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


def _pause() -> None:
    try:
        input("\n按回车键返回主菜单...")
    except (EOFError, KeyboardInterrupt):
        print()


def _print_menu() -> None:
    local = version.read_local(PROJECT_ROOT)
    print("\n" + "=" * 58)
    print("                 Kemo 网关更新工具")
    print("=" * 58)
    print(f"当前版本：{local.version}    Kemo 协议：{local.protocol_version}")
    print("\n  1. 检查并安装更新（推荐）")
    print("  2. 只检查更新，不修改文件")
    print("  3. 查看或恢复更新前备份")
    print("  4. 修复网关源码（高级操作）")
    print("  0. 退出")


def _interactive_backups() -> int:
    backups = backup.list_backups(PROJECT_ROOT)
    print("\n[KEMO] 更新前备份")
    if not backups:
        print("[KEMO] 当前没有可恢复的备份。")
        return 0

    print(f"[KEMO] 找到 {len(backups)} 份备份：")
    for index, backup_id in enumerate(backups, start=1):
        label = "（最新）" if index == 1 else ""
        print(f"  {index}. {backup_id} {label}")
    print("  0. 返回主菜单")

    valid = {str(index) for index in range(len(backups) + 1)}
    choice = _read_choice("请选择要恢复的备份", valid=valid)
    if choice == "0":
        return 0
    return _restore_backup(backups[int(choice) - 1])


def _interactive_menu() -> int:
    """Beginner-facing entry point used when no command suffix is supplied."""
    while True:
        _print_menu()
        choice = _read_choice(
            "请选择操作",
            valid={"0", "1", "2", "3", "4"},
            default="1",
        )
        if choice == "0":
            print("[KEMO] 已退出，未修改任何文件。")
            return 0
        if choice == "1":
            return _apply(yes=False)
        if choice == "2":
            result = _check_cmd()
            _pause()
            if result != 0:
                print("[提示] 检查没有完成，可以稍后重试。")
            continue
        if choice == "3":
            result = _interactive_backups()
            _pause()
            if result != 0:
                print("[提示] 备份恢复没有完成，请检查上面的错误说明。")
            continue

        print("\n[WARN] 修复会重新对齐 Git 已跟踪的网关源码。")
        print("[WARN] 只有源码损坏或普通更新明确提示无法更新时才使用。")
        if not _confirm("是否进入源码修复流程？", default=False):
            print("[KEMO] 已取消修复。")
            continue
        return _repair(yes=False)


def _check(
    local: version.VersionInfo | None = None,
) -> tuple[int, version.VersionInfo | None, version.VersionInfo | None]:
    """Fetch and report the exact local/remote relationship without mutation."""
    local = local or version.read_local(PROJECT_ROOT)
    print(f"[KEMO] 本地版本: {local.version}  (protocol {local.protocol_version})")
    if local.notes:
        print(f"[KEMO] 本地说明: {local.notes}")

    print("[KEMO] 正在检测远程更新...")
    ok, mirror_label = git.fetch(PROJECT_ROOT)
    if not ok:
        print(f"[ERROR] 无法连接远程仓库: {mirror_label}")
        return 1, local, None
    print(f"[KEMO] 远程源: {mirror_label}")

    target_commit = git.get_fetch_commit(PROJECT_ROOT)
    state = git.get_sync_state(PROJECT_ROOT)
    if not target_commit or state.relation == "unknown":
        print("[ERROR] 无法确认 FETCH_HEAD 与本地提交关系。")
        return 1, local, None

    remote = version.read_remote(PROJECT_ROOT)
    if remote is None:
        print("[ERROR] 无法读取远程版本信息。")
        return 1, local, None
    print(f"[KEMO] 远程版本: {remote.version}  (protocol {remote.protocol_version})")
    if remote.notes:
        print(f"[KEMO] 更新说明: {remote.notes}")

    if state.relation == "up_to_date":
        print("[KEMO] 当前已是最新版本。")
    elif state.relation == "behind":
        print(f"[KEMO] 远端新增 {state.behind} 个提交。")
    elif state.relation == "ahead":
        print(
            f"[WARN] 本地领先远端 {state.ahead} 个提交；"
            "普通更新不会覆盖本地提交。"
        )
    else:
        print(
            f"[WARN] 本地与远端已经分叉：本地独有 {state.ahead} 个提交，"
            f"远端独有 {state.behind} 个提交。"
        )

    if state.behind:
        logs = git.get_commit_log(PROJECT_ROOT)
        if logs:
            print(f"[KEMO] 更新内容 ({len(logs)} 个提交):")
            for log in logs:
                print(f"      {log}")

        diff = git.get_remote_diff(PROJECT_ROOT)
        protected_diff = git.get_protected_remote_diff(PROJECT_ROOT)
        if diff.has_changes:
            print(f"[KEMO] 可更新源码 ({len(diff.files)} 个):")
            for file_name in diff.files:
                print(f"      - {file_name}")
        if protected_diff.has_changes:
            print(f"[WARN] 受保护路径变更 ({len(protected_diff.files)} 个):")
            for file_name in protected_diff.files:
                print(f"      - {file_name}")
            print("[WARN] 普通更新和修复都会拒绝该远端提交。")

    comparison = version.compare(local, remote)
    if comparison > 0:
        print(f"[KEMO] 版本差异: {local.version} → {remote.version}")
    elif comparison < 0:
        print(
            f"[WARN] 本地版本 {local.version} 高于远端版本 {remote.version}。"
        )
    elif state.behind:
        print("[KEMO] 版本号相同，但远端包含补丁提交。")

    compatible, message = version.check_protocol_compatibility(local, remote)
    if message:
        print(f"[KEMO] {message}" if compatible else f"[WARN] {message}")
    return 0, local, remote


def _check_cmd() -> int:
    code, _, _ = _check()
    return code


def _reject_protected_remote_changes() -> bool:
    protected = git.get_protected_remote_diff(PROJECT_ROOT)
    if not protected.has_changes:
        return False
    print("[ERROR] 远端差异涉及受保护路径，已拒绝操作：")
    for file_name in protected.files:
        print(f"      - {file_name}")
    print("[ERROR] 本地配置、Provider、密钥、统计和开发目录均未改动。")
    return True


def _install_deps() -> bool:
    if deps.install_requirements(PROJECT_ROOT):
        print("[KEMO] 依赖安装成功。")
        return True
    print("[ERROR] 依赖安装失败，请运行 python setup.py 修复部署。")
    return False


def _build_frontend() -> bool:
    ok, message = frontend.build_frontend(PROJECT_ROOT)
    if ok:
        print(f"[KEMO] 前端构建成功: {message}")
        return True
    print(f"[ERROR] {message}")
    print("[ERROR] 前端构建未完成，请运行 python setup.py 修复部署。")
    return False


def _restore_stash(stashed: bool) -> bool:
    if not stashed:
        return True
    print("[KEMO] 正在恢复本地修改...")
    if git.stash_pop(PROJECT_ROOT):
        return True
    print("[ERROR] 本地修改恢复发生冲突；stash 已保留，请手动处理。")
    return False


def _do_update(
    local: version.VersionInfo,
    remote: version.VersionInfo,
    *,
    is_repair: bool,
    yes: bool,
    diff: git.GitDiff,
    target_commit: str,
) -> int:
    """Execute backup, source transition, local restore and deployment steps."""
    mode_name = "源码修复" if is_repair else "安全更新"
    print(f"\n[KEMO] 开始{mode_name}...")
    print("[KEMO] 正在创建更新前冷备份...")
    backup_ok, backup_message = backup.create(PROJECT_ROOT)
    print(f"[KEMO] {backup_message}")
    if not backup_ok:
        print("[ERROR] 冷备份未完成，操作已中止；Git 工作树尚未修改。")
        return 2

    operation_label = git.get_stash_label()
    stashed = False
    if git.has_local_changes(PROJECT_ROOT):
        print("[KEMO] 检测到本地未提交源码，准备暂存已跟踪和未跟踪文件。")
        if not yes and not _confirm("暂存本地源码后继续？", default=True):
            print("[KEMO] 已取消。冷备份仍然保留。")
            return 0
        stashed = git.stash_local(PROJECT_ROOT, operation_label)
        if not stashed:
            print("[ERROR] 本地源码暂存失败，操作已中止。")
            return 2
        print(f"[KEMO] 本地源码已暂存 (stash: {operation_label})")

    before_commit = git.get_current_commit(PROJECT_ROOT)
    recovery_ref: str | None = None
    if is_repair:
        recovery_ref = git.create_recovery_ref(
            PROJECT_ROOT,
            label=operation_label,
            commit=before_commit,
        )
        if recovery_ref is None:
            print("[ERROR] 无法创建 Git 恢复引用，拒绝执行强制修复。")
            _restore_stash(stashed)
            return 2
        print(f"[KEMO] 已创建 Git 恢复引用: {recovery_ref}")

    if is_repair:
        print(f"[KEMO] 正在将已跟踪源码修复到 {target_commit[:12]}...")
        transitioned = git.hard_reset_to_fetch_head(
            PROJECT_ROOT,
            expected_commit=target_commit,
        )
    else:
        print(f"[KEMO] 正在快进到已验证提交 {target_commit[:12]}...")
        transitioned = git.fast_forward_to_fetch_head(
            PROJECT_ROOT,
            expected_commit=target_commit,
        )

    if not transitioned:
        print(f"[ERROR] {mode_name}失败。")
        if not _restore_stash(stashed):
            return 5
        return 2

    after_commit = git.get_current_commit(PROJECT_ROOT)
    if after_commit != target_commit:
        print("[ERROR] 更新后的 HEAD 与已验证目标提交不一致。")
        if not _restore_stash(stashed):
            return 5
        return 2

    if not _restore_stash(stashed):
        print(f"[ERROR] 远端源码已应用；可使用冷备份或 {recovery_ref or 'stash'} 恢复。")
        return 5

    needs_dependencies = is_repair or deps.requirements_changed(diff.files)
    if needs_dependencies:
        print(
            "[KEMO] 修复模式正在重新校验依赖..."
            if is_repair
            else "[KEMO] requirements.txt 已更新，正在安装依赖..."
        )
        if not _install_deps():
            return 6
    else:
        print("[KEMO] 依赖无变化，跳过。")

    needs_frontend = is_repair or frontend.frontend_changed(diff.files)
    if needs_frontend:
        print(
            "[KEMO] 修复模式正在重新构建前端..."
            if is_repair
            else "[KEMO] 前端源码已更新，正在重新构建..."
        )
        if not _build_frontend():
            return 6
    else:
        print("[KEMO] 前端无变化，跳过构建。")

    print(f"\n[KEMO] {mode_name}完成: {before_commit[:12]} → {after_commit[:12]}")
    print(f"[KEMO] 版本 {local.version} → {remote.version}")
    if remote.notes:
        print(f"[KEMO] 更新说明: {remote.notes}")
    if recovery_ref:
        print(f"[KEMO] 本次修复恢复引用: {recovery_ref}")
    if git.has_local_changes(PROJECT_ROOT):
        print("[KEMO] 本地未提交源码已经恢复。")
    print("\n[KEMO] 网关未运行时执行：python start_web.py")
    print("[KEMO] 网关正在运行时执行：python restart.py")
    return 0


def _apply(yes: bool = False) -> int:
    """Apply only a verified fast-forward update; never imply repair."""
    code, local, remote = _check()
    if code != 0 or local is None or remote is None:
        return code or 1

    state = git.get_sync_state(PROJECT_ROOT)
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
    if _reject_protected_remote_changes():
        return 4

    diff = git.get_remote_diff(PROJECT_ROOT)
    target_commit = git.get_fetch_commit(PROJECT_ROOT)
    if not target_commit:
        print("[ERROR] FETCH_HEAD 无有效提交，已中止。")
        return 1
    if not yes and not _confirm("是否执行安全更新？", default=False):
        print("[KEMO] 已取消。")
        return 0
    return _do_update(
        local,
        remote,
        is_repair=False,
        yes=yes,
        diff=diff,
        target_commit=target_commit,
    )


def _repair(yes: bool = False) -> int:
    """Explicitly restore tracked source from the inspected remote commit."""
    code, local, remote = _check()
    if code != 0 or local is None or remote is None:
        return code or 1
    if _reject_protected_remote_changes():
        return 4

    state = git.get_sync_state(PROJECT_ROOT)
    target_commit = git.get_fetch_commit(PROJECT_ROOT)
    if state.relation == "unknown" or not target_commit:
        print("[ERROR] 无法确认修复目标，已中止。")
        return 1

    print("[WARN] 修复只重置 Git 已跟踪源码；受保护数据不会由远端覆盖。")
    if state.ahead:
        print(
            f"[WARN] 本地有 {state.ahead} 个远端不存在的提交；"
            "修复前会创建 Git 恢复引用。"
        )
    if not yes and not _confirm(
        f"确认将已跟踪源码修复到 {target_commit[:12]}？",
        default=False,
    ):
        print("[KEMO] 已取消。")
        return 0
    return _do_update(
        local,
        remote,
        is_repair=True,
        yes=yes,
        diff=git.get_remote_diff(PROJECT_ROOT),
        target_commit=target_commit,
    )


def _status() -> int:
    """显示本地版本与精确 Git 同步状态。"""
    code, _, _ = _check()
    if code != 0:
        return code
    state = git.get_sync_state(PROJECT_ROOT)
    print(f"[KEMO] Git 状态: {state.relation} (ahead={state.ahead}, behind={state.behind})")
    print(f"[KEMO] 分支: {git.get_current_branch(PROJECT_ROOT)}")
    print(f"[KEMO] 提交: {git.get_current_commit(PROJECT_ROOT)[:12]}")
    return 0


def _list_backups() -> int:
    backups = backup.list_backups(PROJECT_ROOT)
    if not backups:
        print("[KEMO] 没有可用的备份。")
        return 0
    print(f"[KEMO] 可用备份 ({len(backups)} 个):")
    for backup_id in backups:
        print(f"      .backup/{backup_id}/")
    print("\n[KEMO] 普通用户请运行 python update.py，然后选择“查看或恢复备份”。")
    return 0


def _restore_backup(backup_id: str) -> int:
    print(f"[KEMO] 准备从 .backup/{backup_id}/ 恢复同名文件。")
    print("[WARN] 恢复会覆盖同名文件，但不会删除备份中不存在的文件。")
    if not _confirm("确定要恢复吗？", default=False):
        print("[KEMO] 已取消恢复。")
        return 0
    ok, message = backup.restore(PROJECT_ROOT, backup_id)
    print(f"[KEMO] {message}")
    if not ok:
        return 3
    print("[KEMO] 请重启网关使恢复生效。")
    return 0


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
        help="从冷备份恢复同名文件，参数为时间戳或 latest",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过 --apply 或 --repair 的交互确认",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)
    if not effective_argv:
        return _interactive_menu()
    if args.yes and not (args.apply or args.repair):
        parser.error("--yes 只能与 --apply 或 --repair 一起使用")
    if args.check:
        return _check_cmd()
    if args.apply:
        return _apply(yes=args.yes)
    if args.repair:
        return _repair(yes=args.yes)
    if args.status:
        return _status()
    if args.list_backups:
        return _list_backups()
    if args.restore_backup:
        return _restore_backup(args.restore_backup)
    return _check_cmd()


if __name__ == "__main__":
    raise SystemExit(main())
