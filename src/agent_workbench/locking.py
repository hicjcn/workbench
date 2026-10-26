"""Shared/exclusive process locks with the same contention contract on both hosts."""
import os

if os.name == "nt":
    import portalocker
    from portalocker.portalocker import Win32Locker

    LOCK_EX = portalocker.LOCK_EX
    LOCK_SH = portalocker.LOCK_SH
    LOCK_NB = portalocker.LOCK_NB
    LOCK_UN = portalocker.LOCK_UN
    # LockFileEx supports shared locks and a real blocking wait. msvcrt.locking
    # alone cannot preserve the API/Runner shared vs. maintenance exclusive gate.
    _backend = Win32Locker()

    def flock(file, operation):
        try:
            if operation == LOCK_UN:
                _backend.unlock(file)
            else:
                _backend.lock(file, operation)
        except portalocker.exceptions.AlreadyLocked as error:
            raise BlockingIOError("File is locked by another operation") from error
else:
    from fcntl import LOCK_EX, LOCK_SH, LOCK_NB, LOCK_UN, flock
