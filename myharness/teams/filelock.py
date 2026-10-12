"""FileLock：并发文件修改保护。

核心功能：
1. 跨平台文件锁（Windows msvcrt / Unix fcntl）
2. 上下文管理器（with 语句）
3. 自动超时与释放

设计：
- 锁文件：{workspace}/.locks/{file_hash}.lock
- 支持 Windows 和 Unix 两套 API
- 获取锁失败时抛出异常
"""

import hashlib
import platform
import time
from pathlib import Path
from typing import Optional


class FileLock:
    """
    文件锁（跨平台）。

    用法：
        with FileLock(workspace, "path/to/file.py"):
            # 独占访问该文件
            ...
    """

    def __init__(
        self,
        workspace: Path,
        file_path: str,
        timeout: float = 10.0,
    ):
        """
        初始化文件锁。

        Args:
            workspace: 工作区根目录
            file_path: 要锁定的文件路径（相对于 workspace）
            timeout: 获取锁的超时时间（秒）
        """
        self.workspace = workspace
        self.file_path = file_path
        self.timeout = timeout

        # 锁文件路径：使用文件路径的哈希避免路径冲突
        file_hash = hashlib.md5(file_path.encode()).hexdigest()
        self.lock_dir = workspace / ".locks"
        self.lock_file = self.lock_dir / f"{file_hash}.lock"
        self._lock_handle: Optional[object] = None

    def __enter__(self):
        """获取锁。"""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """释放锁。"""
        self.release()

    def acquire(self) -> None:
        """
        获取锁。

        Raises:
            TimeoutError: 超时未获取到锁
            OSError: 锁定失败
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        start_time = time.time()
        system = platform.system()

        while time.time() - start_time < self.timeout:
            try:
                if system == "Windows":
                    self._acquire_windows()
                else:
                    self._acquire_unix()
                return
            except (OSError, IOError):
                # 锁被占用，等待重试
                time.sleep(0.1)

        raise TimeoutError(
            f"Failed to acquire lock for {self.file_path} "
            f"within {self.timeout} seconds"
        )

    def release(self) -> None:
        """释放锁。"""
        if self._lock_handle is None:
            return

        system = platform.system()
        try:
            if system == "Windows":
                self._release_windows()
            else:
                self._release_unix()
        except Exception as exc:
            print(f"[filelock] Failed to release lock: {exc}")
        finally:
            self._lock_handle = None

    def _acquire_windows(self) -> None:
        """Windows 平台获取锁（msvcrt.locking）。"""
        import msvcrt

        # 打开锁文件
        handle = open(self.lock_file, "w", encoding="utf-8")
        try:
            # 尝试独占锁定
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._lock_handle = handle
        except (OSError, IOError):
            handle.close()
            raise

    def _release_windows(self) -> None:
        """Windows 平台释放锁。"""
        import msvcrt

        if self._lock_handle:
            try:
                msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._lock_handle.close()

    def _acquire_unix(self) -> None:
        """Unix 平台获取锁（fcntl.flock）。"""
        import fcntl

        # 打开锁文件
        handle = open(self.lock_file, "w", encoding="utf-8")
        try:
            # 尝试独占锁定（非阻塞）
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_handle = handle
        except (OSError, IOError):
            handle.close()
            raise

    def _release_unix(self) -> None:
        """Unix 平台释放锁。"""
        import fcntl

        if self._lock_handle:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
