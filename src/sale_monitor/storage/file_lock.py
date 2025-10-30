class FileLock:
    """A simple file locking mechanism to prevent concurrent access."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock_file = f"{filepath}.lock"

    def acquire(self):
        """Acquire a lock on the file."""
        while True:
            try:
                # Try to create a lock file
                self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                # Wait and retry if the lock file already exists
                time.sleep(0.1)

    def release(self):
        """Release the lock on the file."""
        os.close(self.lock_fd)
        os.remove(self.lock_file)