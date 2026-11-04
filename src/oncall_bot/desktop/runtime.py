"""OS resources shared by the desktop entry points and the legacy CLI."""

import contextlib
import os
import sys
from pathlib import Path


def data_root():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OnCallAgent"
    return (
        Path.home() / "Library/Application Support/OnCallAgent"
        if sys.platform == "darwin"
        else (Path.home() / ".oncall-desktop")
    )


@contextlib.contextmanager
def instance_lock():
    root = data_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "engine.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有引擎正在运行，请先退出旧实例") from exc
        yield


def launch_args(mode):
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if os.name == "nt":
            executable = executable.with_name("OnCallEngine.exe")
        return [str(executable), "--" + mode]
    return [sys.executable, "-m", "oncall_bot.desktop", "--" + mode]
