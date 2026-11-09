"""A per-state-directory ownership lock held until the server fully closes."""

import fcntl
import os


BUSY_MESSAGE = "本地工作台正在运行但暂时无响应或仍在启动。请稍后重试；不会启动第二个后台，也不会重试模型请求。"


class StateOwner:
    def __init__(self, directory):
        self.descriptor = None
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory / "instance.lock", flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def state_is_owned(directory):
    try:
        with StateOwner(directory):
            return False
    except BlockingIOError:
        return True


def process_is_alive(pid):
    # Includes legacy pre-lock owners: do not replace an old live installation on timeout.
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
