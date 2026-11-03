import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None
        self.locked_pid = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Другой экземпляр уже работает") from None
        self.locked_pid = os.getpid()
        return self

    def owns(self, path):
        return bool(
            self.locked_pid == os.getpid()
            and self.handle is not None
            and not self.handle.closed
            and self.path.resolve() == Path(path).resolve()
        )

    def __exit__(self, *args):
        self.locked_pid = None
        self.handle.close()
