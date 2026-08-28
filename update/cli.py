"""Kemo 网关更新器的唯一应用入口。

命令行参数和无参数菜单都调用同一组检查、计划、事务和恢复函数。这样小白
用户可以直接运行 ``python update.py``，自动化环境则可以使用显式参数，二者
不会维护两套行为。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from update import backup, checker, git, integrity, plan, ui, version
from update._utils import UpdateError, redact_text
from update.constants import DEFAULT_BRANCH, ROOT
from update.transaction import perform_update


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def _print_menu(project_root: Path) -> None:
    local = version.read_local(project_root)
    print("\n" + "=" * 68)
    print("                     Kemo 网关更新中心")
    print("=" * 68)
    print(f"当前版本：{local.version}    Kemo 协议：{local.protocol_version}")
    if local.notes:
        print(f"当前说明：{local.notes}")
    print("\n  1. 检查并安装更新（推荐）")
    print("  2. 只检查远程更新（不修改文件）")
    print("  3. 查看或恢复更新前备份")
    print("  4. 修复网关源码（高级操作）")
    print("  5. 预览更新计划（不改工作树；会刷新 Git 检查引用）")
    print("  6. 查看当前版本与源码状态")
    print("  0. 退出")


def _check(
    project_root: Path,
    *,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> tuple[int, version.VersionInfo | None, version.VersionInfo | None]:
    if branch == DEFAULT_BRANCH and remote_url is None and remote_version_url is None:
        # 保留旧调用方的精确调用形态，也让测试和第三方脚本无需理解新参数。
        return checker.check(project_root)
    return checker.check(
        project_root,
        branch=branch,
        remote_url=remote_url,
        remote_version_url=remote_version_url,
    )


def check_command(
    project_root: Path,
    *,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> int:
    code, _, _ = _check(
        project_root,
        branch=branch,
        remote_url=remote_url,
        remote_version_url=remote_version_url,
    )
    return code


def _print_remote_safety(local: version.VersionInfo, remote: version.VersionInfo) -> bool:
    comparison = version.compare(local, remote)
    if comparison < 0:
        print(
            f"[ERROR] 拒绝自动降级：本地版本 {local.version} 高于远程版本 {remote.version}。"
        )
        print("[ERROR] 如需恢复旧版本，请使用“查看或恢复更新前备份”。")
        return False
    compatible, message = version.check_protocol_compatibility(local, remote)
    if not compatible:
        print(f"[ERROR] {message}")
        print("[ERROR] Kemo 协议主版本不兼容，已停止自动更新。")
        return False
    if message:
        print(f"[KEMO] {message}")
    return True


def apply(
    project_root: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    force: bool = False,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> int:
    """执行安全快进更新；``dry_run`` 只展示计划，不触碰工作树。"""

    code, local, remote = _check(
        project_root,
        branch=branch,
        remote_url=remote_url,
        remote_version_url=remote_version_url,
    )
    if code != 0 or local is None or remote is None:
        return code or 1

    source_state = integrity.check_source_state(project_root)
    if not source_state.ok:
        print(f"[ERROR] {source_state.message}：")
        for file_name in source_state.files:
            print(f"      - {file_name}")
        print("[ERROR] 请先选择“修复网关源码”，普通更新不会覆盖不确定状态。")
        return 5
    if not _print_remote_safety(local, remote):
        return 4

    state = git.get_sync_state(project_root)
    if state.relation == "up_to_date" and not force:
        print("[KEMO] 当前已经是最新提交；未修改任何文件。")
        return 0
    if state.relation == "ahead":
        print("[ERROR] 本地领先远端，普通更新已拒绝；不会丢弃本地提交。")
        return 4
    if state.relation == "diverged":
        print("[ERROR] 本地与远端分叉，普通更新已拒绝；请先人工确认历史。")
        return 4
    if state.relation not in {"behind", "up_to_date"}:
        print("[ERROR] 无法确认安全快进条件。")
        return 1
    if checker.reject_protected_remote_changes(project_root):
        return 4

    diff = git.get_remote_diff(project_root)
    target_commit = git.get_fetch_commit(project_root)
    if not target_commit:
        print("[ERROR] FETCH_HEAD 没有有效提交，已中止。")
        return 1
    if not diff.has_changes and not force:
        print("[KEMO] 远端没有可更新的源码文件；未修改任何文件。")
        return 0

    if diff.files:
        print(f"[KEMO] 更新计划：{len(diff.files)} 个源码文件")
        for file_name in diff.files[:60]:
            print(f"      - {file_name}")
        if len(diff.files) > 60:
            print(f"      ... 其余 {len(diff.files) - 60} 个文件未展开")
    if not dry_run and not yes and not ui.confirm("确认按上述计划执行更新？", default=True):
        print("[KEMO] 已取消，未修改任何文件。")
        return 0

    kwargs = {
        "is_repair": False,
        "yes": yes,
        "diff": diff,
        "target_commit": target_commit,
    }
    if dry_run:
        kwargs["dry_run"] = True
    if force:
        kwargs["force"] = True
    return perform_update(project_root, local, remote, **kwargs)


def repair(
    project_root: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> int:
    """显式将已跟踪源码修复到最近一次 fetch 锁定的提交。"""

    code, local, remote = _check(
        project_root,
        branch=branch,
        remote_url=remote_url,
        remote_version_url=remote_version_url,
    )
    if code != 0 or local is None or remote is None:
        return code or 1
    # 干跑只跳过真正的写操作；受保护路径检查仍必须执行，避免给用户
    # 展示一个看似可行、实际会被拒绝的修复计划。
    if checker.reject_protected_remote_changes(project_root):
        return 4

    state = git.get_sync_state(project_root)
    target_commit = git.get_fetch_commit(project_root)
    if not target_commit or state.relation == "unknown":
        print("[ERROR] 无法确认修复目标，已中止。")
        return 1

    print("[警告] 修复会重新对齐 Git 已跟踪源码；私有配置、Provider、密钥、统计数据不会覆盖。")
    print("[警告] 本地源码会先保存到 .backup/，修复后的源码不会自动再合并本地修改。")
    if not dry_run and not yes and not ui.confirm(
        f"确认将源码修复到 {target_commit[:12]}？", default=False
    ):
        print("[KEMO] 已取消修复。")
        return 0

    kwargs = {
        "is_repair": True,
        "yes": yes,
        "diff": git.get_remote_diff(project_root),
        "target_commit": target_commit,
    }
    if dry_run:
        kwargs["dry_run"] = True
    return perform_update(project_root, local, remote, **kwargs)


def status(
    project_root: Path,
    *,
    branch: str = DEFAULT_BRANCH,
    remote_url: str | None = None,
    remote_version_url: str | None = None,
) -> int:
    code, _, _ = _check(
        project_root,
        branch=branch,
        remote_url=remote_url,
        remote_version_url=remote_version_url,
    )
    if code != 0:
        return code
    state = git.get_sync_state(project_root)
    current_commit = git.get_current_commit(project_root)
    print(f"[KEMO] Git 状态：{state.relation}（ahead={state.ahead}, behind={state.behind}）")
    print(f"[KEMO] 分支：{git.get_current_branch(project_root)}")
    print(f"[KEMO] 当前提交：{current_commit[:12] if current_commit else 'unknown'}")
    source_state = integrity.check_source_state(project_root)
    print(f"[KEMO] 源码完整性：{'正常' if source_state.ok else source_state.message}")
    return 0 if source_state.ok else 5


def list_backups(project_root: Path) -> int:
    items = backup.list_backups(project_root)
    if not items:
        print("[KEMO] 没有可用的更新备份。")
        return 0
    print(f"[KEMO] 可用更新备份（{len(items)} 个）：")
    for backup_id in items:
        print(f"      .backup/{backup_id}/")
    print("[KEMO] 运行 python update.py 并选择“查看或恢复更新前备份”可进行恢复。")
    return 0


def restore_backup(project_root: Path, backup_id: str, *, yes: bool = False) -> int:
    print(f"[KEMO] 准备从 .backup/{backup_id}/ 恢复源码文件。")
    print("[警告] .env、Provider 密钥、统计数据库、运行时数据和开发目录不会覆盖。")
    if not yes and not ui.confirm("确定要恢复吗？", default=False):
        print("[KEMO] 已取消恢复。")
        return 0
    ok, message = backup.restore(project_root, backup_id)
    print(f"[KEMO] {message}")
    if not ok:
        return 3
    source_state = integrity.check_source_state(project_root)
    if not source_state.ok:
        print(f"[ERROR] 恢复后的源码仍不完整：{source_state.message}")
        return 5
    print("[KEMO] 恢复完成，请重启网关使源码生效。")
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
    choice = ui.read_choice(
        "请选择要恢复的备份",
        valid={str(index) for index in range(len(items) + 1)},
    )
    if choice == "0":
        return 0
    return restore_backup(project_root, items[int(choice) - 1])


def preview(project_root: Path) -> int:
    """展示一次结构化更新计划，不进入写入事务。"""

    print("[KEMO] 预览会执行只读 Git fetch，刷新 FETCH_HEAD 和远程检查引用；")
    print("[KEMO] 不会修改工作树、配置、密钥或运行数据。")
    code, inspection = plan.collect(project_root)
    ui.title("Kemo 更新计划预览")
    for line in plan.render(inspection):
        print(f"  {line}")
    if inspection.remote is None:
        print("[提示] 尚未获得远程版本，无法继续预览构建步骤。")
        return code or 1
    if inspection.protected_diff.has_changes:
        print("[错误] 远程提交触及受保护路径，本次更新将被拒绝。")
        for file_name in inspection.protected_diff.files:
            print(f"      - {file_name}")
        return 4
    if not inspection.source_diff.files:
        print("[KEMO] 没有可更新的源码文件。")
        return 0
    print(f"  依赖：{'更新 requirements.txt' if any(name == 'requirements.txt' for name in inspection.source_diff.files) else '不变'}")
    print(
        "  前端："
        + (
            "重新构建"
            if any(
                name.startswith("web/frontend/")
                for name in inspection.source_diff.files
            )
            else "不变"
        )
    )
    print("  事务：创建 .backup/ 冷备份，安全快进，失败自动恢复")
    return 0


def interactive_menu(project_root: Path) -> int:
    while True:
        _print_menu(project_root)
        choice = ui.read_choice(
            "请选择操作",
            valid={"0", "1", "2", "3", "4", "5", "6"},
            default="1",
        )
        if choice == "0":
            print("[KEMO] 已退出，未修改任何文件。")
            return 0
        if choice == "1":
            # 保留“直接运行 update.py 默认执行推荐更新”的小白用户合同。
            return apply(project_root)
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
        if choice == "4":
            print("\n[警告] 只有源码损坏或普通更新明确提示无法更新时才使用修复。")
            if not ui.confirm("是否进入源码修复流程？", default=False):
                print("[KEMO] 已取消修复。")
                continue
            return repair(project_root)
        if choice == "5":
            result = preview(project_root)
            ui.pause()
            if result != 0:
                print("[提示] 预览没有完成，请检查上面的错误说明。")
            continue
        result = status(project_root)
        ui.pause()
        if result != 0:
            print("[提示] 状态检查没有完成，可以稍后重试。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kemo 网关安全更新系统；不带参数时进入交互菜单"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="只检查远端状态")
    actions.add_argument("--apply", action="store_true", help="执行安全快进更新")
    actions.add_argument("--repair", action="store_true", help="显式修复 Git 已跟踪源码")
    actions.add_argument("--status", action="store_true", help="显示版本与 Git 状态")
    actions.add_argument("--list-backups", action="store_true", help="列出更新前备份")
    actions.add_argument(
        "--restore-backup",
        type=str,
        metavar="TIME",
        help="从备份恢复源码，参数为时间戳或 latest",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="跳过写入操作的确认")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览更新，不修改工作树、配置或运行数据（会刷新 Git 检查引用）",
    )
    parser.add_argument("--force", action="store_true", help="版本相同或无差异时仍重跑更新流程")
    parser.add_argument("--repo-url", default=None, help="本次操作使用的远程仓库 URL，不改写 origin")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="远程分支，默认 main")
    parser.add_argument("--remote-version-url", default=None, help="只读版本文件 URL 备用地址")
    return parser


def main(argv: list[str] | None = None, *, project_root: Path = ROOT) -> int:
    _configure_utf8_console()
    parser = build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)
    if args.yes and not (args.apply or args.repair or args.restore_backup):
        parser.error("--yes 只能与 --apply、--repair 或 --restore-backup 一起使用")
    if args.dry_run and not (args.apply or args.repair):
        parser.error("--dry-run 只能与 --apply 或 --repair 一起使用")
    if args.force and not (args.apply or args.repair):
        parser.error("--force 只能与 --apply 或 --repair 一起使用")

    try:
        if not effective_argv:
            return interactive_menu(project_root)
        if args.check:
            return check_command(
                project_root,
                branch=args.branch,
                remote_url=args.repo_url,
                remote_version_url=args.remote_version_url,
            )
        if args.apply:
            return apply(
                project_root,
                yes=args.yes,
                dry_run=args.dry_run,
                force=args.force,
                branch=args.branch,
                remote_url=args.repo_url,
                remote_version_url=args.remote_version_url,
            )
        if args.repair:
            return repair(
                project_root,
                yes=args.yes,
                dry_run=args.dry_run,
                branch=args.branch,
                remote_url=args.repo_url,
                remote_version_url=args.remote_version_url,
            )
        if args.status:
            return status(
                project_root,
                branch=args.branch,
                remote_url=args.repo_url,
                remote_version_url=args.remote_version_url,
            )
        if args.list_backups:
            return list_backups(project_root)
        if args.restore_backup:
            return restore_backup(project_root, args.restore_backup, yes=args.yes)
        return check_command(
            project_root,
            branch=args.branch,
            remote_url=args.repo_url,
            remote_version_url=args.remote_version_url,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] 外部命令失败：{redact_text(exc)}", file=sys.stderr)
        return 1
    except UpdateError as exc:
        print(f"[ERROR] {redact_text(exc)}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[ERROR] 文件系统操作失败：{redact_text(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[KEMO] 已中断，未继续执行后续操作。", file=sys.stderr)
        return 130
