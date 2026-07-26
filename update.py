"""Kemo 网关更新系统入口。

用法：
  python update.py --check        只检查新版本
  python update.py --apply        全量更新并重启
  python update.py --status       查看当前版本信息
  python update.py --rollback     回滚到上一个 commit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from update import git, version, deps, frontend

PROJECT_ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _check() -> int:
    """只检查远程是否有新版本，不做任何修改。"""
    print("[KEMO] 正在检查更新...")

    local = version.read_local(PROJECT_ROOT)
    print(f"[KEMO] 本地版本: {local.version}  (protocol {local.protocol_version})")
    if local.notes:
        print(f"[KEMO] 本地说明: {local.notes}")

    if not git.fetch(PROJECT_ROOT):
        print("[ERROR] 无法连接远程仓库，请检查网络或 Git 配置。")
        return 1

    if not git.has_remote_commits(PROJECT_ROOT):
        print("[KEMO] 当前已是最新版本。")
        return 0

    remote = version.read_remote(PROJECT_ROOT)
    if remote:
        print(f"[KEMO] 远程版本: {remote.version}  (protocol {remote.protocol_version})")
        if remote.notes:
            print(f"[KEMO] 更新说明: {remote.notes}")

    diff = git.get_remote_diff(PROJECT_ROOT)
    if diff.has_changes:
        print(f"[KEMO] 涉及 {len(diff.files)} 个文件变更:")
        for f in diff.files:
            print(f"      - {f}")
    else:
        print("[KEMO] 变更文件均在排除范围（厂商包/提示词/密钥），无需更新。")

    cmp = version.compare(local, remote) if remote else 0
    if cmp > 0:
        print(f"[KEMO] 发现新版本 {remote.version}，可执行 --apply 更新。")
        return 0
    elif cmp == 0:
        print("[KEMO] 版本号相同但有新提交（补丁更新），可执行 --apply。")
        return 0
    return 0


def _apply() -> int:
    """执行全量更新：暂存 → 拉取 → 构建 → 重启。"""
    print("[KEMO] 开始更新...")

    # 1. 检查远程
    if not git.fetch(PROJECT_ROOT):
        print("[ERROR] 无法连接远程仓库。")
        return 1

    if not git.has_remote_commits(PROJECT_ROOT):
        print("[KEMO] 当前已是最新版本，无需更新。")
        return 0

    local = version.read_local(PROJECT_ROOT)
    remote = version.read_remote(PROJECT_ROOT)
    print(f"[KEMO] {local.version} → {remote.version if remote else '?'}")

    # 2. 获取变更清单
    diff = git.get_remote_diff(PROJECT_ROOT)
    if not diff.has_changes:
        print("[KEMO] 变更均在排除范围，无需更新操作。")
        return 0

    print(f"[KEMO] 变更文件 ({len(diff.files)} 个):")
    for f in diff.files:
        print(f"      - {f}")

    # 3. 暂存本地修改
    label = git.get_stash_label()
    had_local = git.stash_local(PROJECT_ROOT, label)
    if had_local:
        print(f"[KEMO] 本地修改已暂存 (stash: {label})")

    # 4. 拉取
    before_commit = git.get_current_commit(PROJECT_ROOT)
    print(f"[KEMO] 正在拉取更新...")
    if not git.pull(PROJECT_ROOT):
        print("[ERROR] 拉取失败，正在回滚...")
        if had_local:
            git.stash_pop(PROJECT_ROOT)
        return 2

    # 5. 恢复本地修改
    if had_local:
        print(f"[KEMO] 正在恢复本地修改...")
        if not git.stash_pop(PROJECT_ROOT):
            print("[WARN] 本地修改恢复失败（可能有冲突），请手动处理。")

    # 6. 安装依赖
    if deps.requirements_changed(diff.files):
        print("[KEMO] requirements.txt 已更新，正在安装依赖...")
        if not deps.install_requirements(PROJECT_ROOT):
            print("[WARN] 依赖安装失败，请手动运行 pip install -r requirements.txt")
    else:
        print("[KEMO] 依赖无变化，跳过。")

    # 7. 前端构建
    if frontend.frontend_changed(diff.files):
        print("[KEMO] 前端代码已更新，正在构建...")
        ok, msg = frontend.build_frontend(PROJECT_ROOT)
        if ok:
            print(f"[KEMO] 前端构建成功: {msg}")
        else:
            print(f"[WARN] {msg}")
            print("[WARN] 前端未更新，网关后端仍可正常运行。")
    else:
        print("[KEMO] 前端无变化，跳过构建。")

    # 8. 完成
    new_commit = git.get_current_commit(PROJECT_ROOT)
    print(f"[KEMO] 更新完成: {before_commit[:8]} → {new_commit[:8]}")

    if remote:
        print(f"[KEMO] 版本 {local.version} → {remote.version}")
        print(f"[KEMO] 更新说明: {remote.notes}")
    print("[KEMO] 请重启网关使更新生效（python restart.py）")
    return 0


def _status() -> int:
    """显示当前版本信息。"""
    local = version.read_local(PROJECT_ROOT)
    print(f"版本: {local.version}")
    print(f"协议版本: {local.protocol_version}")
    print(f"说明: {local.notes or '无'}")
    print(f"提交: {git.get_current_commit(PROJECT_ROOT)[:12]}")
    print(f"远程: ", end="")
    if git.fetch(PROJECT_ROOT):
        if git.has_remote_commits(PROJECT_ROOT):
            remote = version.read_remote(PROJECT_ROOT)
            if remote:
                print(f"{remote.version} (可更新)")
            else:
                print("有新提交 (可更新)")
        else:
            print("已是最新")
    else:
        print("无法连接远程")
    return 0


def _rollback() -> int:
    """回滚到上一个 commit。"""
    before = git.get_current_commit(PROJECT_ROOT)
    print(f"[KEMO] 当前: {before[:8]}")
    print(f"[KEMO] 正在回滚到上一个 commit...")
    if not git.rollback(PROJECT_ROOT):
        print("[ERROR] 回滚失败。")
        return 3
    after = git.get_current_commit(PROJECT_ROOT)
    print(f"[KEMO] 回滚完成: {before[:8]} → {after[:8]}")
    print("[KEMO] 请重启网关使回滚生效。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kemo 网关更新系统")
    parser.add_argument("--check", action="store_true", help="只检查新版本")
    parser.add_argument("--apply", action="store_true", help="全量更新")
    parser.add_argument("--status", action="store_true", help="显示版本信息")
    parser.add_argument("--rollback", action="store_true", help="回滚到上一个 commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        return _check()
    if args.apply:
        return _apply()
    if args.status:
        return _status()
    if args.rollback:
        return _rollback()
    # 无参数时默认检查
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())
