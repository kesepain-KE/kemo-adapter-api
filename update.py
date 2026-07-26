"""Kemo 网关更新系统入口。

用法：
  python update.py --check        只检查新版本
  python update.py --apply        交互式更新（先展示变更，确认后执行）
  python update.py --apply --yes  跳过确认，直接更新
  python update.py --status       查看当前版本信息
  python update.py --rollback     回滚到上一个 commit
  python update.py --list-backups 列出所有可用备份
  python update.py --restore-backup latest  从最新备份恢复
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from update import git, version, deps, frontend, backup

PROJECT_ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _confirm(prompt: str, default: bool = False) -> bool:
    """交互式询问用户确认。default=True 时默认为 y。"""
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{prompt} [{hint}] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not raw:
            return default
        if raw.lower() in ("y", "yes"):
            return True
        if raw.lower() in ("n", "no"):
            return False
        print(f"请输入 y 或 n。")


def _check(local: version.VersionInfo | None = None) -> tuple[int, version.VersionInfo | None, version.VersionInfo | None]:
    """检测更新，返回 (结果码, 本地版本, 远程版本)。"""
    local = local or version.read_local(PROJECT_ROOT)
    print(f"[KEMO] 本地版本: {local.version}  (protocol {local.protocol_version})")
    if local.notes:
        print(f"[KEMO] 本地说明: {local.notes}")

    print(f"[KEMO] 正在检测远程更新...")
    ok, mirror_label = git.fetch(PROJECT_ROOT)
    if not ok:
        print(f"[ERROR] 无法连接远程仓库: {mirror_label}")
        return 1, local, None

    print(f"[KEMO] 远程源: {mirror_label}")

    if not git.has_remote_commits(PROJECT_ROOT):
        print("[KEMO] 当前已是最新版本。")
        return 0, local, None

    remote = version.read_remote(PROJECT_ROOT)
    if not remote:
        print("[ERROR] 无法读取远程版本信息。")
        return 1, local, None

    print(f"[KEMO] 远程版本: {remote.version}  (protocol {remote.protocol_version})")
    if remote.notes:
        print(f"[KEMO] 更新说明: {remote.notes}")

    # 提交日志
    logs = git.get_commit_log(PROJECT_ROOT)
    if logs:
        print(f"[KEMO] 更新内容 ({len(logs)} 个提交):")
        for log in logs:
            print(f"      {log}")

    # 变更文件
    diff = git.get_remote_diff(PROJECT_ROOT)
    if diff.has_changes:
        print(f"[KEMO] 非排除文件变更 ({len(diff.files)} 个):")
        for f in diff.files:
            print(f"      - {f}")
    else:
        print("[KEMO] 变更均在排除范围（厂商包/提示词/密钥/备份），无需更新。")

    # 版本比对
    cmp = version.compare(local, remote)
    if cmp > 0:
        print(f"[KEMO] 版本差异: {local.version} → {remote.version}")
    elif cmp == 0:
        print(f"[KEMO] 版本差异: 版本号相同，但有补丁提交。")

    # 协议兼容性检测
    compat, compat_msg = version.check_protocol_compatibility(local, remote)
    if compat_msg:
        if compat:
            print(f"[KEMO] {compat_msg}")
        else:
            print(f"[WARN] {compat_msg}")

    return 0, local, remote


def _check_cmd() -> int:
    code, _, _ = _check()
    return code


def _apply(yes: bool = False) -> int:
    """全量更新：展示变更 → 确认 → 暂存 → 拉取 → 构建 → 提示重启。"""
    code, local, remote = _check()
    if code != 0:
        return code
    if remote is None:
        return 0

    # 更新确认
    if not yes:
        if not _confirm("是否执行更新？", default=False):
            print("[KEMO] 已取消更新。")
            return 0

    diff = git.get_remote_diff(PROJECT_ROOT)
    if not diff.has_changes:
        print("[KEMO] 无可更新的非排除文件，跳过。")
        return 0

    # ========== 开始执行更新 ==========
    print("\n[KEMO] 开始执行更新...")

    # 1. 创建更新前备份
    print(f"[KEMO] 正在创建更新前备份...")
    backup_ok, backup_msg = backup.create(PROJECT_ROOT)
    print(f"[KEMO] {backup_msg}")

    # 2. 暂存本地修改
    has_local = git.has_local_changes(PROJECT_ROOT)
    stashed = False
    if has_local:
        label = git.get_stash_label()
        print(f"[KEMO] 检测到本地未提交修改，正在暂存...")
        if not yes:
            if not _confirm("暂存本地修改后继续？", default=True):
                print("[KEMO] 已取消更新。")
                return 0
        stashed = git.stash_local(PROJECT_ROOT, label)
        if stashed:
            print(f"[KEMO] 本地修改已暂存 (stash: {label})")
        else:
            print("[WARN] 没有需要暂存的本地修改。")

    # 2. 拉取
    before_commit = git.get_current_commit(PROJECT_ROOT)
    before_branch = git.get_current_branch(PROJECT_ROOT)
    print(f"[KEMO] 分支: {before_branch}, 当前提交: {before_commit[:12]}")

    # 获取镜像源 URL 用于 pull（如果 fetch 用了镜像）
    remote_url = None
    mirror_env = __import__("os").environ.get("GIT_MIRROR", "")
    if mirror_env:
        origin_url = git._resolve_remote_url(PROJECT_ROOT)
        if origin_url:
            remote_url = git._mirror_url(origin_url, mirror_env)

    print("[KEMO] 正在拉取更新...")
    if not git.pull(PROJECT_ROOT, remote_url):
        print("[ERROR] 拉取失败。")
        # 尝试恢复 stash
        if stashed:
            print("[KEMO] 正在恢复本地修改...")
            git.stash_pop(PROJECT_ROOT)
        return 2

    # 3. 恢复本地修改
    if stashed:
        print("[KEMO] 正在恢复本地修改...")
        if not git.stash_pop(PROJECT_ROOT):
            print("[WARN] 本地修改恢复失败（可能有冲突），请手动 git stash pop 处理。")

    # 4. 安装依赖
    if deps.requirements_changed(diff.files):
        print("[KEMO] requirements.txt 已更新，正在安装依赖...")
        if not yes:
            if not _confirm("是否安装新依赖？", default=True):
                print("[KEMO] 跳过依赖安装。")
            else:
                _install_deps()
        else:
            _install_deps()
    else:
        print("[KEMO] 依赖无变化，跳过。")

    # 5. 前端构建
    if frontend.frontend_changed(diff.files):
        print("[KEMO] 前端代码已更新，正在构建...")
        if not yes:
            if not _confirm("是否构建前端？", default=True):
                print("[KEMO] 跳过前端构建。")
            else:
                _build_frontend()
        else:
            _build_frontend()
    else:
        print("[KEMO] 前端无变化，跳过构建。")

    # 6. 完成
    new_commit = git.get_current_commit(PROJECT_ROOT)
    print(f"\n[KEMO] 更新完成: {before_commit[:12]} → {new_commit[:12]}")
    print(f"[KEMO] 版本 {local.version} → {remote.version}")
    if remote.notes:
        print(f"[KEMO] 更新说明: {remote.notes}")

    # 检查本地是否有未处理修改
    if git.has_local_changes(PROJECT_ROOT):
        print("[KEMO] 注意：本地仍有未提交修改。")

    print("[KEMO] 请重启网关使更新生效（python restart.py）")
    return 0


def _install_deps() -> None:
    if deps.install_requirements(PROJECT_ROOT):
        print("[KEMO] 依赖安装成功。")
    else:
        print("[WARN] 依赖安装失败，请手动运行 pip install -r requirements.txt")


def _build_frontend() -> None:
    ok, msg = frontend.build_frontend(PROJECT_ROOT)
    if ok:
        print(f"[KEMO] 前端构建成功: {msg}")
    else:
        print(f"[WARN] {msg}")
        print("[WARN] 前端未更新，网关后端仍可正常运行。")


def _status() -> int:
    """显示当前版本信息。"""
    local = version.read_local(PROJECT_ROOT)
    print(f"版本: {local.version}")
    print(f"协议版本: {local.protocol_version}")
    print(f"说明: {local.notes or '无'}")
    print(f"分支: {git.get_current_branch(PROJECT_ROOT)}")
    print(f"提交: {git.get_current_commit(PROJECT_ROOT)[:12]}")

    print("\n检测远程更新...")
    ok, mirror_label = git.fetch(PROJECT_ROOT)
    if ok:
        if git.has_remote_commits(PROJECT_ROOT):
            remote = version.read_remote(PROJECT_ROOT)
            if remote:
                print(f"远程: {remote.version} (可更新)")
            else:
                print("远程: 有新提交 (可更新)")
        else:
            print("远程: 已是最新")
        print(f"镜像源: {mirror_label}")
    else:
        print(f"远程: 无法连接 ({mirror_label})")
    return 0


def _rollback() -> int:
    """回滚到上一个 commit。"""
    before = git.get_current_commit(PROJECT_ROOT)
    print(f"[KEMO] 当前提交: {before[:12]}")
    print(f"[KEMO] 目标: HEAD 的上一个 commit")

    if not _confirm("确定要回滚吗？", default=False):
        print("[KEMO] 已取消回滚。")
        return 0

    print("[KEMO] 正在回滚...")
    if not git.rollback(PROJECT_ROOT):
        print("[ERROR] 回滚失败。")
        return 3
    after = git.get_current_commit(PROJECT_ROOT)
    if before == after:
        print("[KEMO] 没有更早的 commit，已是最初状态。")
        return 0
    print(f"[KEMO] 回滚完成: {before[:12]} → {after[:12]}")
    print("[KEMO] 请重启网关使回滚生效。")
    return 0


def _list_backups() -> int:
    """列出所有可用冷备份。"""
    backups = backup.list_backups(PROJECT_ROOT)
    if not backups:
        print("[KEMO] 没有可用的备份。")
        return 0
    print(f"[KEMO] 可用备份 ({len(backups)} 个):")
    for b in backups:
        print(f"      .backup/{b}/")
    print(f"\n[KEMO] 恢复方式: python update.py --restore-backup <时间戳>")
    return 0


def _restore_backup(backup_id: str) -> int:
    """从冷备份恢复整个项目。"""
    print(f"[KEMO] 正在从 .backup/{backup_id}/ 恢复...")
    print("[WARN] 恢复会覆盖当前项目的全部文件！")

    if not _confirm("确定要恢复吗？", default=False):
        print("[KEMO] 已取消恢复。")
        return 0

    ok, msg = backup.restore(PROJECT_ROOT, backup_id)
    print(f"[KEMO] {msg}")
    if ok:
        print("[KEMO] 请重启网关使恢复生效。")
        return 0
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kemo 网关更新系统")
    parser.add_argument("--check", action="store_true", help="只检查新版本")
    parser.add_argument("--apply", action="store_true", help="全量更新（交互式确认）")
    parser.add_argument("--yes", action="store_true", help="跳过所有交互确认（配合 --apply）")
    parser.add_argument("--status", action="store_true", help="显示版本信息")
    parser.add_argument("--rollback", action="store_true", help="回滚到上一个 commit")
    parser.add_argument("--list-backups", action="store_true", help="列出所有可用备份")
    parser.add_argument("--restore-backup", type=str, metavar="TIME",
                        help="从备份恢复，参数为备份时间戳（Y-m-d-H-M-S）或 latest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        return _check_cmd()
    if args.apply:
        return _apply(yes=args.yes)
    if args.status:
        return _status()
    if args.rollback:
        return _rollback()
    if args.list_backups:
        return _list_backups()
    if args.restore_backup:
        return _restore_backup(args.restore_backup)
    # 无参数时默认检查
    return _check_cmd()


if __name__ == "__main__":
    raise SystemExit(main())
