"""One isolated Claude process group per task. Only explicit project tools exist."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import signal
import stat
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, create_sdk_mcp_server, tool

from ..runner.turn_stream import TurnStream

LIMIT = 256 * 1024


@contextlib.contextmanager
def directory(root, parts, identity=None):
    """Walk via directory handles so a concurrently replaced symlink cannot redirect I/O."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        st = os.fstat(fd)
        if identity and (st.st_dev, st.st_ino) != identity:
            raise ValueError("目录已替换")
        for part in parts:
            if part.startswith(".") or part in ("", "/"):
                raise ValueError("路径无权限")
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def project_io(root, identity, path, operation, content=""):
    if os.name == "nt":
        from .windows import project_io as win_io

        return win_io(root, identity, path, operation, content, LIMIT)
    parts = Path(path).parts
    if Path(path).is_absolute() or any(p.startswith(".") for p in parts):
        raise ValueError("只允许项目内非隐藏路径")
    if operation == "list":
        with directory(root, parts, identity) as fd:
            return "\n".join(sorted(n for n in os.listdir(fd) if not n.startswith("."))[:500])
    if not parts:
        raise ValueError("缺少文件名")
    with directory(root, parts[:-1], identity) as fd:
        name = parts[-1]
        if operation == "read":
            raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(raw, "rb") as f:
                info = os.fstat(raw)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("只允许普通文件")
                return f.read(LIMIT).decode("utf-8")
        if operation != "write" or len(content.encode()) > LIMIT:
            raise ValueError("无法写入")
        try:
            existing = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise ValueError("目标不是独立普通文件")
            mode = stat.S_IMODE(existing.st_mode) & 0o777
        except FileNotFoundError:
            mode = 0o600
        # Replace instead of truncating a path which could have become a link.
        tmp = ".oncall-" + secrets.token_hex(12)
        try:
            raw = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode, dir_fd=fd)
            with os.fdopen(raw, "wb") as f:
                f.write(content.encode())
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=fd)
        return "已写入"


def project_tools(snap, inputs):
    @tool(
        "project",
        "List a directory or read a UTF-8 file relative to this project.",
        {"action": str, "path": str},
    )
    async def project(args):
        if args["action"] not in ("list", "read"):
            raise ValueError("未知读取操作")
        return reply(project_io(snap["path"], (snap["dev"], snap["ino"]), args["path"], args["action"]))

    @tool("edit", "Write a UTF-8 file inside this project.", {"path": str, "content": str})
    async def edit(args):
        return reply(
            project_io(snap["path"], (snap["dev"], snap["ino"]), args["path"], "write", args["content"])
        )

    @tool("input", "Read one of the input filenames listed in the request.", {"name": str})
    async def input_file(args):
        if args["name"] not in inputs:
            raise ValueError("附件不属于本轮")
        file = Path(inputs[args["name"]])
        if file.suffix == ".md":
            return reply(file.read_text(encoding="utf-8")[:200000])
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(file.suffix)
        if not mime:
            raise ValueError("不支持此图片格式")
        return {
            "content": [
                {"type": "image", "mimeType": mime, "data": base64.b64encode(file.read_bytes()).decode()}
            ]
        }

    return [project] + ([edit] if snap["edit"] else []) + ([input_file] if inputs else [])


def reply(text):
    return {"content": [{"type": "text", "text": text}]}


async def run_turn():
    data = json.loads(sys.stdin.buffer.readline())
    s, inputs = data["snapshot"], data["inputs"]
    available = project_tools(s, inputs)
    opts = ClaudeAgentOptions(
        cwd=s["path"],
        cli_path=data["cli"],
        session_id=s["sid"] if data["fresh"] else None,
        resume=None if data["fresh"] else s["sid"],
        tools=[],
        permission_mode="dontAsk",
        mcp_servers={"project": create_sdk_mcp_server(name="project", tools=available)},
        allowed_tools=["mcp__project__" + t.name for t in available],
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        max_turns=60,
        settings=json.dumps({"disableAllHooks": True}),
        extra_args={"disable-slash-commands": None},
    )
    async with ClaudeSDKClient(options=opts) as client:
        loop = asyncio.get_running_loop()
        while True:
            interrupted = False
            interrupt_task = None

            def interrupt():
                nonlocal interrupted, interrupt_task
                if not interrupted:
                    interrupted = True
                    interrupt_task = loop.create_task(client.interrupt())

            if os.name != "nt":
                loop.add_signal_handler(signal.SIGUSR1, interrupt)
            prompt = data["snapshot"]["text"]
            if inputs:
                prompt += "\n本轮输入文件（使用 input 工具读取）：" + ", ".join(inputs)
            await client.query(prompt)
            async for msg in TurnStream().iterate(client.receive_messages()):
                if isinstance(msg, ResultMessage):
                    if msg.session_id != s["sid"]:
                        raise RuntimeError("会话 ID 不匹配")
                    state = (
                        "cancelled"
                        if interrupted and (msg.terminal_reason or "").startswith("aborted")
                        else "failed"
                        if msg.is_error
                        else "succeeded"
                    )
                    print(
                        json.dumps(
                            {
                                "state": state,
                                "result": msg.result or "本轮结束",
                                "reusable": not inputs and not interrupted,
                            }
                        ),
                        flush=True,
                    )
                    break
                print(json.dumps({"progress": type(msg).__name__}), flush=True)
            else:
                raise RuntimeError("缺少终态")
            if interrupt_task:
                await interrupt_task
            if inputs or interrupted:
                return
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                return
            data = json.loads(line)
            # Defense in depth: a retained client cannot switch session, root, or permissions.
            if (
                any(
                    data["snapshot"][k] != s[k]
                    for k in ("sid", "path", "revision", "generation", "edit", "ino", "dev")
                )
                or data["inputs"]
            ):
                raise ValueError("连接上下文发生变化")


def main():
    try:
        asyncio.run(run_turn())
    except Exception:
        # stderr may contain prompts or tokens. The engine only exposes a generic failure.
        return 1
    return 0
