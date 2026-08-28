"""更新事务：备份、精确提交切换、本地修改恢复、构建、校验和回滚。"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from update import backup, deps, frontend, git, integrity
from update._utils import UpdateError, git_control_dir, redact_text
from update.git import GitDiff
from update.lock import UpdateLock, UpdateLockError
from update.ui import confirm
from update.version import VersionInfo


def _print_integrity_failure(result: integrity.IntegrityResult) -> None:
    print(f"[ERROR] {result.message}")
    for item in result.files:
        print(f"      - {item}")


def _install_deps(project_root: Path) -> bool:
    if deps.install_requirements(project_root):
        print("[KEMO] 依赖安装成功。")
        return True
    print("[ERROR] 依赖安装失败，请运行 python setup.py 修复部署。")
    return False


def _build_frontend(project_root: Path) -> bool:
    ok, message = frontend.build_frontend(project_root)
    if ok:
        print(f"[KEMO] 前端构建成功: {message}")
        return True
    print(f"[ERROR] {message}")
    print("[ERROR] 前端构建未完成，请运行 python setup.py 修复部署。")
    return False


@contextmanager
def _maintenance_marker(project_root: Path):
    """在更新期间发布一个可诊断的维护标记，并保证异常时清理。"""

    control_dir = git_control_dir(project_root)
    marker_parent = control_dir or project_root.resolve()
    marker = marker_parent / ".update.maintenance"
    payload = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "reason": "gateway_update",
    }
    try:
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"无法写入更新维护标记：{redact_text(exc)}") from exc
    try:
        yield
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _rollback(
    project_root: Path,
    *,
    before_commit: str,
    diff: GitDiff,
    stash_commit: str | None,
    backup_id: str | None,
) -> bool:
    """恢复 Git HEAD、更新涉及的未跟踪文件和原始本地修改。"""

    print("[KEMO] 更新未通过完整性检查，正在自动恢复更新前源码...")
    git.abort_in_progress_operations(project_root)
    restored = git.reset_to_commit(project_root, before_commit)
    git.clean_update_paths(project_root, diff.files)

    if stash_commit:
        if git.apply_stash(project_root, stash_commit):
            print("[KEMO] 原有本地修改已经恢复；安全 stash 仍保留。")
        else:
            git.abort_in_progress_operations(project_root)
            git.reset_to_commit(project_root, before_commit)
            git.clean_update_paths(project_root, diff.files)
            restored = False

    if not restored and backup_id:
        backup_ok, backup_message = backup.restore(project_root, backup_id)
        print(f"[KEMO] {backup_message}")
        restored = backup_ok

    state = integrity.check_source_state(project_root)
    if not state.ok:
        _print_integrity_failure(state)
        return False
    if git.get_current_commit(project_root) != before_commit:
        print("[ERROR] 自动恢复后的 HEAD 与更新前提交不一致。")
        return False
    print("[KEMO] 已恢复到更新前可启动源码；未覆盖私有配置和运行数据。")
    return restored


def perform_update(
    project_root: Path,
    local: VersionInfo,
    remote: VersionInfo,
    *,
    is_repair: bool,
    yes: bool,
    diff: GitDiff,
    target_commit: str,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """执行一个单实例、可回滚的更新或显式源码修复。"""

    mode_name = "源码修复" if is_repair else "安全更新"
    if dry_run:
        print(f"\n[KEMO] 预览{mode_name}（不会修改工作树、源码、配置或运行数据）")
        print(f"[KEMO] 目标提交: {target_commit[:12] if target_commit else '未知'}")
        print(f"[KEMO] 预计更新文件: {len(diff.files)} 个")
        for file_name in diff.files[:40]:
            print(f"      - {file_name}")
        if len(diff.files) > 40:
            print(f"      ... 其余 {len(diff.files) - 40} 个文件未展开")
        print(
            "[KEMO] 依赖处理: "
            + ("将安装 requirements.txt" if is_repair or force or deps.requirements_changed(diff.files) else "跳过")
        )
        print(
            "[KEMO] 前端处理: "
            + ("将重新构建 Web 前端" if is_repair or force or frontend.frontend_changed(diff.files) else "跳过")
        )
        print("[KEMO] 预览完成。若确认执行，请运行 python update.py 或 python update.py --apply。")
        return 0

    before_commit = ""
    backup_id: str | None = None
    stash_commit: str | None = None
    transitioned = False
    rollback_done = False

    def fail_with_rollback(code: int) -> int:
        """统一失败出口，确保一次事务最多执行一次回滚。"""

        nonlocal rollback_done
        if rollback_done:
            return code
        rollback_done = True
        restored = _rollback(
            project_root,
            before_commit=before_commit,
            diff=diff,
            stash_commit=stash_commit,
            backup_id=backup_id,
        )
        return code if restored else 7

    try:
        with UpdateLock(project_root), _maintenance_marker(project_root):
            initial_state = integrity.check_source_state(project_root)
            if not is_repair and not initial_state.ok:
                _print_integrity_failure(initial_state)
                print("[ERROR] 普通更新已停止。请运行 python update.py 并选择“修复网关源码”。")
                return 5

            print(f"\n[KEMO] 开始{mode_name}...")
            print("[KEMO] 正在创建更新前冷备份...")
            backup_ok, backup_message, backup_id = backup.create_snapshot(project_root)
            print(f"[KEMO] {backup_message}")
            if not backup_ok:
                print("[ERROR] 冷备份未完成，操作已中止；Git 工作树尚未修改。")
                return 2

            before_commit = git.get_current_commit(project_root)
            if not before_commit:
                print("[ERROR] 无法读取更新前 Git 提交，操作已中止。")
                return 2

            operation_label = git.get_stash_label()
            if is_repair:
                print("[WARN] 修复模式会重新对齐已跟踪源码；现有源码已保存在冷备份中。")
            elif git.has_local_changes(project_root):
                print("[KEMO] 检测到本地未提交源码，准备创建精确安全 stash。")
                if not yes and not confirm("暂存本地源码后继续？", default=True):
                    print("[KEMO] 已取消。冷备份仍然保留。")
                    return 0
                stash_commit = git.create_stash(project_root, operation_label)
                if not stash_commit:
                    print("[ERROR] 本地源码暂存失败，操作已中止。")
                    return 2
                print(f"[KEMO] 本地源码已暂存: {stash_commit[:12]}")

            recovery_ref: str | None = None
            if is_repair:
                recovery_ref = git.create_recovery_ref(
                    project_root,
                    label=operation_label,
                    commit=before_commit,
                )
                if recovery_ref is None:
                    print("[ERROR] 无法创建 Git 恢复引用，拒绝执行强制修复。")
                    return 2
                print(f"[KEMO] 已创建 Git 恢复引用: {recovery_ref}")

            if is_repair:
                print(f"[KEMO] 正在将已跟踪源码修复到 {target_commit[:12]}...")
                git.abort_in_progress_operations(project_root)
                transitioned = git.hard_reset_to_fetch_head(
                    project_root,
                    expected_commit=target_commit,
                )
            else:
                print(f"[KEMO] 正在快进到已验证提交 {target_commit[:12]}...")
                transitioned = git.fast_forward_to_fetch_head(
                    project_root,
                    expected_commit=target_commit,
                )

            if not transitioned or git.get_current_commit(project_root) != target_commit:
                print(f"[ERROR] {mode_name}未能切换到锁定提交。")
                return fail_with_rollback(2)

            if stash_commit:
                print("[KEMO] 正在恢复本地修改...")
                if not git.apply_stash(project_root, stash_commit):
                    print("[ERROR] 本地修改与新版本冲突，本次更新不会留下 UU 文件。")
                    return fail_with_rollback(5)

            needs_dependencies = is_repair or force or deps.requirements_changed(diff.files)
            if needs_dependencies:
                print(
                    "[KEMO] 修复模式正在重新校验依赖..."
                    if is_repair
                    else "[KEMO] requirements.txt 已更新，正在安装依赖..."
                )
                if not _install_deps(project_root):
                    return fail_with_rollback(6)
            else:
                print("[KEMO] 依赖无变化，跳过。")

            needs_frontend = is_repair or force or frontend.frontend_changed(diff.files)
            if needs_frontend:
                print(
                    "[KEMO] 修复模式正在重新构建前端..."
                    if is_repair
                    else "[KEMO] 前端源码已更新，正在重新构建..."
                )
                if not _build_frontend(project_root):
                    return fail_with_rollback(6)
            else:
                print("[KEMO] 前端无变化，跳过构建。")

            print("[KEMO] 正在执行冲突、编译、前端和启动完整性检查...")
            validation = integrity.validate_installed_source(
                project_root,
                expected_commit=target_commit,
            )
            if not validation.ok:
                _print_integrity_failure(validation)
                return fail_with_rollback(6)

            if stash_commit and not git.drop_stash(project_root, stash_commit):
                print("[WARN] 更新成功，但安全 stash 未自动删除；可稍后人工清理。")

            after_commit = git.get_current_commit(project_root)
            print(f"\n[KEMO] {mode_name}完成: {before_commit[:12]} → {after_commit[:12]}")
            print(f"[KEMO] 版本 {local.version} → {remote.version}")
            if remote.notes:
                print(f"[KEMO] 更新说明: {remote.notes}")
            if recovery_ref:
                print(f"[KEMO] 本次修复恢复引用: {recovery_ref}")
            print("[KEMO] 完整性检查通过，源码中不存在 Git 冲突标记。")
            print("\n[KEMO] 网关未运行时执行：python start_web.py")
            print("[KEMO] 网关正在运行时执行：python restart.py")
            return 0
    except UpdateLockError as exc:
        print(f"[ERROR] {exc}")
        return 8
    except KeyboardInterrupt:
        if before_commit and transitioned and not rollback_done:
            try:
                fail_with_rollback(130)
            except Exception as rollback_error:
                print(f"[ERROR] 中断后的自动恢复失败：{redact_text(rollback_error)}")
        print("\n[WARN] 更新被中断；已停止后续写入，请检查上方状态和 .backup/ 备份。")
        return 130
    except Exception as exc:
        if before_commit and transitioned and not rollback_done:
            try:
                fail_with_rollback(7)
            except Exception as rollback_error:
                print(f"[ERROR] 自动恢复失败：{redact_text(rollback_error)}")
        print(f"[ERROR] 更新过程出现未处理异常：{redact_text(exc)}")
        print("[ERROR] 未知异常不会被静默忽略，请优先检查 .backup/ 与 Git 状态。")
        return 7

