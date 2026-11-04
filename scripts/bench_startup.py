"""量一下「每轮新建 client」和「长连接复用 client」的启动开销差距。

用极简 prompt（只要求回一个词）把模型思考时间压到最小，剩下的就是启动成本。

用法：
  python scripts/bench_startup.py                     # 用一次性新建的小会话
  python scripts/bench_startup.py --session <id>      # 用指定会话（注意会往它里面加几轮对话）
  python scripts/bench_startup.py --turns 3
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    delete_session,
    fork_session,
    get_session_info,
)

PROMPT = "只回复一个字：好"


def _fork_id(result: object) -> str | None:
    """ForkSessionResult 的字段名以实际 SDK 为准，这里几个常见名都试一下。"""
    for attr in ("session_id", "new_session_id", "forked_session_id", "id"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(result, str):
        return result
    return None


def build_options(workdir: str, session_id: str | None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=workdir,
        resume=session_id,
        fork_session=False,
        permission_mode="bypassPermissions",
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit"],
        setting_sources=["user", "project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "回答必须极简，不要使用任何工具。",
        },
        max_turns=2,
    )


async def _drain(client: ClaudeSDKClient, t0: float) -> tuple[float, float, str]:
    """返回 (首条消息耗时, 整轮耗时, 回复片段)。"""
    first: float | None = None
    text = ""
    async for message in client.receive_response():
        if first is None:
            first = time.monotonic() - t0
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    text += block.text
        if isinstance(message, ResultMessage):
            break
    return (first or 0.0), time.monotonic() - t0, text.strip()[:20]


async def bench_fresh(workdir: str, session_id: str, turns: int) -> list[tuple[float, float]]:
    """当前实现：每轮新建 client。连接耗时也算进去。"""
    results = []
    for i in range(turns):
        t0 = time.monotonic()
        async with ClaudeSDKClient(options=build_options(workdir, session_id)) as client:
            connected = time.monotonic() - t0
            t1 = time.monotonic()
            await client.query(PROMPT)
            first, total, reply = await _drain(client, t1)
        print(f"    第 {i + 1} 轮：连接 {connected:5.2f}s  首条消息 {first:5.2f}s  整轮 {total:5.2f}s  回复={reply!r}")
        results.append((connected, total))
    return results


async def bench_persistent(workdir: str, session_id: str, turns: int) -> list[tuple[float, float]]:
    """候选实现：一个 client 连续跑多轮。连接只付一次。"""
    results = []
    t0 = time.monotonic()
    async with ClaudeSDKClient(options=build_options(workdir, session_id)) as client:
        connected = time.monotonic() - t0
        print(f"    建立连接一次：{connected:5.2f}s")
        for i in range(turns):
            t1 = time.monotonic()
            await client.query(PROMPT)
            first, total, reply = await _drain(client, t1)
            print(f"    第 {i + 1} 轮：首条消息 {first:5.2f}s  整轮 {total:5.2f}s  回复={reply!r}")
            results.append((0.0 if i else connected, total))
    return results


async def make_session(workdir: str) -> str:
    async with ClaudeSDKClient(options=build_options(workdir, None)) as client:
        await client.query("只回复：已就绪")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                return message.session_id
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default=None, help="已有会话 ID；不给就新建一个一次性会话")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument(
        "--fork",
        action="store_true",
        help="先 fork 一份再测，测完删掉。用真实大会话时务必加这个，别污染原会话",
    )
    args = parser.parse_args()

    workdir = str(ROOT)
    throwaway = False
    session_id = args.session

    if session_id:
        info = get_session_info(session_id, None)
        if info is None:
            print(f"找不到会话 {session_id}")
            return 1
        workdir = info.cwd or workdir
        size_mb = (info.file_size or 0) / 1024 / 1024
        print(f"原会话 {session_id[:8]} · {info.custom_title or info.summary}")
        print(f"  目录={workdir}")
        print(f"  历史文件={size_mb:.1f} MB")

        if args.fork:
            result = fork_session(session_id, directory=workdir, title="bench-临时副本")
            forked = _fork_id(result)
            if not forked:
                print(f"  fork 失败，返回值={result!r}")
                return 1
            session_id = forked
            throwaway = True
            forked_info = get_session_info(session_id, workdir)
            forked_mb = (forked_info.file_size or 0) / 1024 / 1024 if forked_info else 0
            print(f"  已 fork 成临时副本 {session_id[:8]}（{forked_mb:.1f} MB），测完会删掉")
        else:
            print("  注意：没加 --fork，这次测量会往原会话里追加几轮对话")
        print()
    else:
        print("新建一次性会话用于测量…")
        session_id = await make_session(workdir)
        if not session_id:
            print("新建会话失败")
            return 1
        throwaway = True
        info = get_session_info(session_id, workdir)
        size_kb = (info.file_size or 0) / 1024 if info else 0
        print(f"  session_id={session_id}  历史文件={size_kb:.0f} KB\n")

    try:
        print(f"A. 每轮新建 client（当前实现），{args.turns} 轮：")
        fresh = await bench_fresh(workdir, session_id, args.turns)

        print(f"\nB. 长连接复用 client，{args.turns} 轮：")
        persistent = await bench_persistent(workdir, session_id, args.turns)
    finally:
        if throwaway:
            delete_session(session_id, directory=workdir)
            print(f"\n已清理一次性会话 {session_id[:8]}")

    # A 每轮都要付连接成本，所以墙钟时间是 连接 + 对话
    fresh_connect = [c for c, _ in fresh]
    fresh_wall = [c + t for c, t in fresh]
    # B 只有第一轮付连接成本
    pers_wall = [c + t for c, t in persistent]
    pers_steady = [t for c, t in persistent if c == 0.0] or [t for _, t in persistent]

    print("\n=== 对比（每轮墙钟时间 = 连接 + 对话）===")
    print(f"  A 每轮连接       中位数 {statistics.median(fresh_connect):5.2f}s  "
          f"（最小 {min(fresh_connect):.2f} 最大 {max(fresh_connect):.2f}）")
    print(f"  A 每轮墙钟       中位数 {statistics.median(fresh_wall):5.2f}s")
    print(f"  B 首轮墙钟       {pers_wall[0]:5.2f}s（含一次性连接）")
    print(f"  B 之后每轮       中位数 {statistics.median(pers_steady):5.2f}s")
    saved = statistics.median(fresh_wall) - statistics.median(pers_steady)
    print(f"  → 长连接稳定后每轮省下 {saved:5.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
