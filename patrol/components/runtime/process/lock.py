"""Stable lock inode: never unlink the lock file, even on release."""
import contextlib
import os
from pathlib import Path
from ...persistence.index import Busy


@contextlib.contextmanager
def instance_lock(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,"a+b") as f:
        if os.name=="nt":
            import msvcrt
            if f.seek(0,os.SEEK_END)==0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                raise Busy("another tick or migration owns the lock") from None
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy("another tick or migration owns the lock") from None
            try:
                yield
            finally:
                fcntl.flock(f,fcntl.LOCK_UN)
