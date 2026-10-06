"""OS-held station lock; released automatically if the process dies."""
import os


class StationLock:
    def __init__(self, path):
        self.file = path.open('a+b')
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another publisher owns this HLS station directory') from None

    def close(self):
        self.file.close()
