"""Reserve a service-owned listener path across concurrent process starts."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


@contextmanager
def reserve(path):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError('Invalid listener directory')
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('The listener directory must be service-owned and writable only by its owner')
    # Keep the lock inode in place. Removing it at exit would allow another
    # process to lock a new inode while a concurrent starter holds the old one.
    lock = os.open(str(path)+'.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if path.is_symlink():
            raise ValueError('Invalid listener path')
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError('Listener path is occupied')
            # An earlier version may already be serving without this lock.
            import socket
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    pass
                else:
                    raise ValueError('Listener is already running')
            path.unlink()
        yield path
    finally:
        os.close(lock)
