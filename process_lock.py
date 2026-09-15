"""Nonblocking advisory process lock for Windows, macOS and Linux."""
import os

LOCK_EX = 2
LOCK_NB = 4


def flock(handle, flags=LOCK_EX | LOCK_NB):
    if os.name != 'nt':
        import fcntl
        return fcntl.flock(handle, flags)
    import msvcrt
    # Windows locks a byte range; all instances lock the first byte.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write('\0')
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError('Another instance holds this ledger lock') from exc
