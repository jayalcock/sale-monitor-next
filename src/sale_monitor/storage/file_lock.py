import fcntl
import os


class FileLock:
    """OS-level file locking using fcntl.flock (POSIX)."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock_file = f"{filepath}.lock"
        self.lock_fd = None

    def acquire(self):
        """Acquire an exclusive lock on the file."""
        self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX)

    def release(self):
        """Release the lock on the file."""
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False