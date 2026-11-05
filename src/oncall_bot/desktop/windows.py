"""Windows-only resources. A task's Job handle lives in its parent engine."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import uuid
from pathlib import Path

import pywintypes
import win32api
import win32con
import win32file
import win32job
import win32process

FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # winbase.h; absent in win32con.


def attach_job(pid):
    # pywin32 requires a Unicode job name. Keep each engine-owned job distinct
    # and prevent children from inheriting the handle that controls its lifetime.
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.bInheritHandle = False
    job = win32job.CreateJobObject(security, f"Local\\OnCallAgent-{uuid.uuid4()}")
    try:
        policy = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        policy["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, policy)
        process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            # The child waits for its first stdin frame until assignment succeeds.
            win32job.AssignProcessToJobObject(job, process)
        finally:
            process.Close()
    except BaseException:
        job.Close()
        raise
    return job


def alive(pid):
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except pywintypes.error as exc:
        return exc.winerror != 87  # Only ERROR_INVALID_PARAMETER proves the PID is gone.
    try:
        return win32process.GetExitCodeProcess(handle) == 259
    finally:
        handle.Close()


def project_io(root, identity, path, operation, content, limit):
    relative = Path(path)
    if relative.anchor or any(
        p.startswith(".") or p.endswith((" ", ".")) or ":" in p or Path(p).is_reserved()
        for p in relative.parts
    ):
        raise ValueError("只允许项目内普通路径")
    target = Path(root) / relative
    base = target if operation == "list" else target.parent
    if not relative.parts and operation != "list":
        raise ValueError("缺少文件名")
    # Pin every ancestor without delete/write sharing. A junction cannot be
    # swapped into the path between validation and opening/replacing the leaf.
    with contextlib.ExitStack() as stack:
        for directory in (*reversed(base.parents), base):
            handle = win32file.CreateFile(
                str(directory),
                0,
                win32con.FILE_SHARE_READ,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            stack.callback(handle.Close)
            attr = win32file.GetFileInformationByHandle(handle)[0]
            if attr & stat.FILE_ATTRIBUTE_REPARSE_POINT or not attr & stat.FILE_ATTRIBUTE_DIRECTORY:
                raise ValueError("不能穿过重解析点")
        st = os.stat(root)
        if identity != (st.st_dev, st.st_ino):
            raise ValueError("目录已替换")
        if operation == "list":
            return "\n".join(sorted(p.name for p in target.iterdir() if not p.name.startswith("."))[:500])
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info and (
            info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise ValueError("只允许独立普通文件")
        if operation == "read":
            handle = win32file.CreateFile(
                str(target),
                win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ,
                None,
                win32con.OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            stack.callback(handle.Close)
            attrs, *rest = win32file.GetFileInformationByHandle(handle)
            if attrs & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY) or rest[6] != 1:
                raise ValueError("文件已替换")
            if not win32file.GetFileSize(handle):
                return ""
            return win32file.ReadFile(handle, limit)[1].decode("utf-8")
        if operation != "write" or len(content.encode()) > limit:
            raise ValueError("无法写入")
        with tempfile.NamedTemporaryFile(dir=base, delete=False) as f:
            stack.callback(Path(f.name).unlink, missing_ok=True)
            f.write(content.encode())
        os.replace(f.name, target)
        return "已写入"
