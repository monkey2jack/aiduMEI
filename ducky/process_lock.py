"""One API process per data directory, held until the process exits.

Do not unlink the lock file: doing so would create a second lock inode. The
descriptor deliberately outlives ASGI shutdown, since a timed-out daemon may
still write after the lifespan has returned.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading

_guard = threading.Lock()
_held: dict[str, tuple[int, int]] = {}


def acquire_api_process_lock(data_dir: str | os.PathLike) -> None:
    directory = Path(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = str(directory / ".api-process.lock")
    with _guard:
        previous = _held.get(path)
        if previous and previous[0] == os.getpid():
            return
        if previous:
            # A forked child must acquire its own lock, not reuse the parent's.
            os.close(previous[1])
            del _held[path]
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                "Cannot own the API data directory: another API process holds "
                "its lock, or the filesystem cannot provide an exclusive lock. "
                "Stop the other process; do not remove the lock file."
            ) from exc
        _held[path] = (os.getpid(), fd)
