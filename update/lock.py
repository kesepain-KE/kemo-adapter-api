"""Windows 与 Linux 共用的跨进程更新锁。"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path


class UpdateLockError(RuntimeError):
    """另一个更新进程已经持有锁。"""


def _try_os_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise BlockingIOError(str(exc)) from exc


def _release_os_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class UpdateLock:
    """依赖操作系统文件锁，进程崩溃时由系统自动释放。"""

    def __init__(self, project_root: Path) -> None:
        git_dir = project_root / ".git"
        state_dir = git_dir if git_dir.is_dir() else project_root
        self.path = state_dir / "kemo-update.lock"
        self._token = uuid.uuid4().hex
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                _try_os_lock(descriptor)
            except BlockingIOError as exc:
                raise UpdateLockError("已有更新正在运行，本次操作已停止。") from exc
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "created_at": time.time(),
                    "token": self._token,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            os.ftruncate(descriptor, max(1, len(payload)))
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            self._fd = descriptor
        except Exception:
            _release_os_lock(descriptor)
            os.close(descriptor)
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _release_os_lock(self._fd)
            os.close(self._fd)
        finally:
            self._fd = None

    def __enter__(self) -> "UpdateLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.release()

