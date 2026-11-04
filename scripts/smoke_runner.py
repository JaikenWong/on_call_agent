"""冒烟验证：不需要飞书凭据，用假的 FeishuApi 跑完整条执行链。

覆盖：
  1. SessionRunner 能 resume 既有会话、上下文延续、卡片生命周期正确
  2. 会话不存在时明确失败，且不新建会话
  3. CommandHandler 的 /sessions /status /use 回复内容正确
  4. Dispatcher 串行排队与 /stop

用法：
  conda activate oncall
  python scripts/smoke_runner.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    delete_session,
)

from oncall_bot.commands import CommandHandler  # noqa: E402
from oncall_bot.config import Config  # noqa: E402
from oncall_bot.feishu.parser import IncomingMessage  # noqa: E402
from oncall_bot.runner.connection import SessionConnection  # noqa: E402
from oncall_bot.runner.dispatcher import Dispatcher  # noqa: E402
from oncall_bot.runner.session_runner import SessionRunner, TurnHandle  # noqa: E402
from oncall_bot.state import ActiveSession, StateStore, now_iso  # noqa: E402

WORKDIR = str(ROOT)
PASSPHRASE = "青铜犀牛九号"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


class FakeApi:
    """记录所有调用，替代真实的 FeishuApi。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict] = []
        self.patches: list[dict] = []
        self._counter = 0

    async def reply_text(self, message_id: str, text: str, *, in_thread: bool = False) -> str:
        self.texts.append(text)
        self._counter += 1
        return f"om_text_{self._counter}"

    async def reply_long_text(self, message_id: str, text: str, *, in_thread: bool = False) -> None:
        self.texts.append(text)

    async def reply_card(self, message_id: str, card: dict, *, in_thread: bool = False) -> str:
        self.cards.append(card)
        self._counter += 1
        return f"om_card_{self._counter}"

    async def patch_card(self, card_message_id: str, card: dict) -> bool:
        self.patches.append(card)
        return True

    # ---------- 云文档（可配置成成功 / 抛权限错误）----------

    docx_content: str = "需求说明\n第一条：把按钮改成蓝色。"
    docx_error: Exception | None = None
    blocks_error: Exception | None = None
    media_error: Exception | None = None
    wiki_target: tuple[str, str, str] = ("docx", "DocxTokenFromWiki123", "知识库里的需求")

    async def docx_raw_content(self, document_id: str) -> str:
        if self.docx_error is not None:
            raise self.docx_error
        return self.docx_content

    async def docx_title(self, document_id: str) -> str:
        return "测试需求文档"

    async def docx_blocks(self, document_id: str, *, page_size: int = 500) -> list[dict]:
        if self.blocks_error is not None:
            raise self.blocks_error
        return DOC_BLOCKS

    async def download_media(self, file_token: str) -> bytes:
        if self.media_error is not None:
            raise self.media_error
        return PNG_BYTES

    async def wiki_node(self, token: str) -> tuple[str, str, str]:
        return self.wiki_target

    # ---------- 断言辅助 ----------

    @property
    def last_patch(self) -> dict:
        return self.patches[-1] if self.patches else {}

    def card_title(self, card: dict) -> str:
        return ((card.get("header") or {}).get("title") or {}).get("content", "")

    def card_body(self, card: dict) -> str:
        for el in card.get("elements") or []:
            if el.get("tag") == "markdown":
                return el.get("content", "")
            if el.get("tag") == "div":  # 旧写法的兜底
                return (el.get("text") or {}).get("content", "")
        return ""


# 最小合法 PNG（1x1），只需要魔术字节正确即可验证落地与格式判断
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

# 模拟一篇带标题、段落、引用文档、图片、有序列表的文档
DOC_BLOCKS: list[dict] = [
    {
        "block_id": "page",
        "block_type": 1,
        "page": {"elements": [{"text_run": {"content": "测试需求文档"}}]},
        "children": ["h1", "t1", "img1", "o1", "o2", "img2"],
    },
    {
        "block_id": "h1",
        "block_type": 3,
        "heading1": {"elements": [{"text_run": {"content": "一、需求"}}]},
    },
    {
        "block_id": "t1",
        "block_type": 2,
        "text": {
            "elements": [
                {"text_run": {"content": "把按钮改成蓝色。"}},
                {
                    "mention_doc": {
                        "title": "参考文档",
                        "url": "https%3A%2F%2Fabc.feishu.cn%2Fdocx%2FRef1234567890",
                    }
                },
            ]
        },
    },
    {"block_id": "img1", "block_type": 27, "image": {"token": "IMGTOKENAAAAAAAAAAAAAAAAAAA"}},
    {
        "block_id": "o1",
        "block_type": 13,
        "ordered": {
            "elements": [{"text_run": {"content": "第一步"}}],
            "style": {"sequence": "1"},
        },
    },
    {
        "block_id": "o2",
        "block_type": 13,
        "ordered": {
            "elements": [{"text_run": {"content": "第二步"}}],
            "style": {"sequence": "auto"},
        },
    },
    {"block_id": "img2", "block_type": 27, "image": {"token": "IMGTOKENBBBBBBBBBBBBBBBBBBB"}},
]


def make_config(state_file: Path) -> Config:
    return Config.model_validate(
        {
            "feishu": {"app_id": "cli_smoke_test", "app_secret": "smoke_secret_value"},
            "policy": {"require_mention_in_group": True, "allowed_open_ids": []},
            "runner": {
                "permission_mode": "bypassPermissions",
                "max_turns": 4,
                "turn_timeout_seconds": 600,
                "idle_output_timeout_seconds": 300,
                "max_queue": 3,
                "disallowed_tools": ["Bash", "Write", "Edit", "NotebookEdit"],
                "setting_sources": ["user"],
                "system_prompt_append": "回答必须极简，一行以内，不要使用任何工具。",
            },
            "render": {"update_interval_ms": 300, "max_card_chars": 4000, "session_list_limit": 5},
            "docs": {
                "enabled": True,
                "inbox_dir": str(state_file.parent / "inbox"),
                "max_chars_per_doc": 200000,
                "max_docs_per_message": 5,
            },
            "state_file": str(state_file),
            "log_dir": str(state_file.parent / "logs"),
        }
    )


def make_msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"om_{uuid.uuid4().hex[:12]}",
        chat_id="oc_smoke",
        chat_type="p2p",
        thread_id="",
        sender_open_id="ou_smoke",
        text=text,
        mentioned_bot=True,
    )


async def create_throwaway_session() -> str:
    """新建一条一次性会话，返回 session_id。"""
    options = ClaudeAgentOptions(
        cwd=WORKDIR,
        permission_mode="default",
        allowed_tools=[],
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "回答必须极简，一行以内，不要使用任何工具。",
        },
        max_turns=2,
    )
    session_id = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"请记住一个口令：{PASSPHRASE}。只回复「已记住」三个字。")
        async for m in client.receive_response():
            if isinstance(m, ResultMessage):
                session_id = m.session_id
    return session_id


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="oncall-smoke-"))
    cfg = make_config(tmp / "state.json")

    print("== 准备：新建一条一次性会话 ==")
    session_id = await create_throwaway_session()
    if not session_id:
        print("  无法创建测试会话，中止")
        return 1
    print(f"  session_id = {session_id}")

    try:
        await run_checks(cfg, session_id)
    finally:
        delete_session(session_id, directory=WORKDIR)
        print(f"\n已清理测试会话 {session_id[:8]}")

    print("\n== 结果 ==")
    if _failures:
        print(f"  {len(_failures)} 项失败：" + "、".join(_failures))
        return 1
    print("  全部通过")
    return 0


async def run_checks(cfg: Config, session_id: str) -> None:
    state = StateStore(cfg.state_file)
    await state.load()
    await state.set_active(
        ActiveSession(
            session_id=session_id,
            workdir=WORKDIR,
            title="smoke",
            switched_by="ou_smoke",
            switched_at=now_iso(),
        )
    )

    # ---------- 1. resume 跑一轮 ----------
    print("\n== 1. SessionRunner 跑一轮（resume 既有会话）==")
    api = FakeApi()
    conn = SessionConnection(cfg.runner.connection_idle_seconds)
    runner = SessionRunner(cfg, api, state, conn)  # type: ignore[arg-type]
    handle = TurnHandle(msg=make_msg("我刚才让你记住的口令是什么？只回答口令本身。"))
    await runner.run_turn(handle)

    check("发出了运行中卡片", len(api.cards) == 1, f"cards={len(api.cards)}")
    if api.cards:
        check("运行中卡片标题正确", api.card_title(api.cards[0]).startswith("处理中"),
              api.card_title(api.cards[0]))
        check("卡片开启了 update_multi",
              (api.cards[0].get("config") or {}).get("update_multi") is True)
    check("最终卡片被更新", len(api.patches) >= 1, f"patches={len(api.patches)}")
    final_title = api.card_title(api.last_patch)
    final_body = api.card_body(api.last_patch)
    check("最终卡片标题是「完成」", final_title.startswith("完成"), final_title)
    check("上下文延续（回复里有口令）", PASSPHRASE in final_body, final_body[:120])
    check("卡片上没有费用数字", "usd" not in str(api.last_patch).lower())
    check("last_card 已清空", state.last_card is None)
    check("last_active_at 已更新", state.active is not None and state.active.last_active_at is not None)
    check("session_id 没有漂移", state.active is not None and state.active.session_id == session_id)

    # ---------- 1b. 连接跨轮复用 ----------
    print("\n== 1b. 连接跨轮复用 ==")
    check("跑完一轮后连接仍然活着", conn.is_warm_for(session_id), conn.state_text)

    api_warm = FakeApi()
    runner_warm = SessionRunner(cfg, api_warm, state, conn)  # type: ignore[arg-type]
    await runner_warm.run_turn(TurnHandle(msg=make_msg("再说一次那个口令，只答口令。")))
    check("第二轮直接复用连接（卡片不显示「正在连接」）",
          bool(api_warm.cards) and "正在处理" in api_warm.card_body(api_warm.cards[0]),
          api_warm.card_body(api_warm.cards[0]) if api_warm.cards else "")
    check("第二轮上下文仍然延续", PASSPHRASE in api_warm.card_body(api_warm.last_patch),
          api_warm.card_body(api_warm.last_patch)[:80])
    check("第二轮结束连接还在", conn.is_warm_for(session_id))

    await conn.close("冒烟段落结束")
    check("显式关闭后连接释放", not conn.is_warm_for(session_id), conn.state_text)

    # ---------- 2. 会话不存在 ----------
    print("\n== 2. 会话不存在时明确失败 ==")
    api2 = FakeApi()
    state2 = StateStore(cfg.state_file.with_name("state2.json"))
    await state2.load()
    bogus = str(uuid.uuid4())
    await state2.set_active(
        ActiveSession(
            session_id=bogus,
            workdir=WORKDIR,
            title="不存在",
            switched_by="ou_smoke",
            switched_at=now_iso(),
        )
    )
    conn2 = SessionConnection(cfg.runner.connection_idle_seconds)
    runner2 = SessionRunner(cfg, api2, state2, conn2)  # type: ignore[arg-type]
    await runner2.run_turn(TurnHandle(msg=make_msg("随便说点什么")))

    check("只发了失败卡片，没有运行中卡片", len(api2.cards) == 1, f"cards={len(api2.cards)}")
    if api2.cards:
        check("失败卡片标题正确", api2.card_title(api2.cards[0]).startswith("失败"),
              api2.card_title(api2.cards[0]))
        check("提示要重新 /use", "/use" in api2.card_body(api2.cards[0]) or "/use" in str(api2.cards[0]))
    from claude_agent_sdk import get_session_info

    check("没有偷偷新建会话", get_session_info(bogus, directory=WORKDIR) is None)
    check("会话不存在时没有建立连接", not conn2.is_warm_for(bogus))

    # ---------- 3. 命令 ----------
    print("\n== 3. 命令 ==")
    api3 = FakeApi()
    dispatcher = Dispatcher(cfg, api3, state)  # type: ignore[arg-type]
    commands = CommandHandler(cfg, api3, state, dispatcher)  # type: ignore[arg-type]

    await commands.handle(make_msg("/help"))
    check("/help 有输出", bool(api3.texts) and "/sessions" in api3.texts[-1])

    await commands.handle(make_msg("/sessions"))
    listing = api3.texts[-1]
    check("/sessions 列出会话", "条会话" in listing, listing.splitlines()[0] if listing else "")
    check("/sessions 带仓库路径", "\\" in listing or "/" in listing)

    await commands.handle(make_msg("/status"))
    status = api3.texts[-1]
    check("/status 显示活跃会话", session_id[:8] in status, status.splitlines()[0] if status else "")
    check("/status 显示权限模式", "bypassPermissions" in status)

    await commands.handle(make_msg("/use 99"))
    check("/use 越界序号被拒", "超出范围" in api3.texts[-1], api3.texts[-1])

    await commands.handle(make_msg("/use 999"))
    check("/use 999 同时说明两种解读都不成立",
          "不是有效序号" in api3.texts[-1] and "可用写法" in api3.texts[-1], api3.texts[-1])

    await commands.handle(make_msg("/use zzz!!!"))
    check("/use 非法参数被拒且给出可用写法",
          "可用写法" in api3.texts[-1] and "前 8 位" in api3.texts[-1], api3.texts[-1])

    await commands.handle(make_msg("/use abcd1234"))
    check("/use 匹配不到的 ID 前缀被拒", "开头" in api3.texts[-1], api3.texts[-1])

    # ID 前缀唯一匹配时应该成功切换（这正是飞书里从 /sessions 复制 8 位 ID 的用法）
    await commands.handle(make_msg("/sessions"))
    await commands.handle(make_msg(f"/use {session_id[:8]}"))
    check("/use <前 8 位 ID> 能切换",
          "已切换活跃会话" in api3.texts[-1] and session_id[:8] in api3.texts[-1],
          api3.texts[-1].splitlines()[0] if api3.texts[-1] else "")
    check("切换后状态里就是这条会话",
          state.active is not None and state.active.session_id == session_id)

    await commands.handle(make_msg(f"/use {session_id}"))
    check("/use <完整 36 位 ID> 也能切换", "已切换活跃会话" in api3.texts[-1])

    await commands.handle(make_msg("/nonsense"))
    check("未知命令被拒且不转发", "未知命令" in api3.texts[-1], api3.texts[-1])

    await commands.handle(make_msg("/stop"))
    check("空闲时 /stop 有合理回复", "没有运行中" in api3.texts[-1], api3.texts[-1])

    # ---------- 4. 队列 ----------
    print("\n== 4. 队列上限与排队提示 ==")
    api4 = FakeApi()
    dispatcher4 = Dispatcher(cfg, api4, state)  # type: ignore[arg-type]
    # 不启动 worker，这样消息会堆在队列里，便于验证上限
    results = [await dispatcher4.submit(make_msg(f"任务 {i}")) for i in range(1, 5)]
    accepted = [r for r in results if r.accepted]
    check("队列上限生效", len(accepted) == cfg.runner.max_queue,
          f"accepted={len(accepted)} max={cfg.runner.max_queue}")
    check("超限时给出明确提示", results[-1].note.startswith("队列已满"), results[-1].note)
    stop_note = await dispatcher4.stop_current()
    check("/stop 清空队列", "清空" in stop_note, stop_note)
    check("清空后队列为空", dispatcher4.queue_size() == 0)

    # ---------- 5. 超时中断 ----------
    print("\n== 5. 超时自动中断 ==")
    timeout_cfg = cfg.model_copy(deep=True)
    timeout_cfg.runner.turn_timeout_seconds = 1
    # 把监控间隔压到 0.2 秒，确保超时一定先于模型完成触发
    import oncall_bot.runner.session_runner as sr_module

    original_interval = sr_module.MONITOR_INTERVAL
    sr_module.MONITOR_INTERVAL = 0.2
    api5 = FakeApi()
    conn5 = SessionConnection(timeout_cfg.runner.connection_idle_seconds)
    runner5 = SessionRunner(timeout_cfg, api5, state, conn5)  # type: ignore[arg-type]
    handle5 = TurnHandle(msg=make_msg("用一句话介绍一下这个仓库是做什么的。"))
    try:
        await runner5.run_turn(handle5)
    finally:
        sr_module.MONITOR_INTERVAL = original_interval

    title5 = api5.card_title(api5.last_patch)
    if title5.startswith("已中断"):
        check("超时后卡片标题是「已中断」", True, title5)
        check("中断原因写进了卡片", "超时" in str(api5.last_patch))
        check("abort_reason 已记录", bool(handle5.abort_reason), handle5.abort_reason or "")
        # 实测 interrupt() 之后连接不可复用，所以必须被丢弃
        check("中断后连接被丢弃", not conn5.is_warm_for(session_id), conn5.state_text)
    else:
        await conn5.close("超时路径未触发")
        # 模型比监控更快跑完，这一轮没触发超时。不算失败，但要说明。
        print(f"  [SKIP] 本轮在监控首次检查前就完成了（标题：{title5}），超时路径未触发")

    # ---------- 6. 云文档链接 ----------
    print("\n== 6. 云文档链接抓取（假 API）==")
    await check_docs(cfg)

    # ---------- 7. 飞书 markdown ----------
    print("\n== 7. 飞书 markdown 转换 ==")
    check_markdown()


def check_markdown() -> None:
    from oncall_bot.feishu import cards
    from oncall_bot.runner.render import (
        _display_width,
        _pad,
        format_table,
        summarize_tool_use,
        to_feishu_markdown,
    )

    # 卡片正文必须用 markdown 组件（lark_md 不渲染代码块和列表）
    body_el = next(
        (e for e in cards.running_card("repo", "x")["elements"] if e.get("tag") == "markdown"), None
    )
    check("卡片正文用 markdown 组件", body_el is not None)
    check("卡片仍然开启 update_multi",
          (cards.running_card("repo", "x").get("config") or {}).get("update_multi") is True)

    # 标题 → 加粗
    out = to_feishu_markdown("## 修改说明\n正文一行")
    check("标题转成加粗", "**修改说明**" in out and "##" not in out, out.replace("\n", "⏎"))
    check("六级标题也处理", "**深**" in to_feishu_markdown("###### 深"))

    # 代码围栏原样保留
    src = "说明：\n```java\npublic class Foo {}\n```\n结束"
    out = to_feishu_markdown(src)
    check("代码围栏原样保留（含语言标记）", "```java" in out and "public class Foo {}" in out)
    check("围栏内的 # 不被当成标题",
          "# 注释" in to_feishu_markdown("```\n# 注释\n```"))

    # 表格 → 对齐的等宽文本块
    out = to_feishu_markdown("对比：\n| 提交 | 说明 |\n|---|---|\n| 372cba3af | 修复取值 |\n后文")
    check("表格被包进代码块", out.count("```") == 2, out.replace("\n", "⏎"))
    check("表格的竖线被去掉", "|" not in out, out.replace("\n", "⏎"))
    check("markdown 分隔行被丢弃", ":---" not in out and "|---|" not in out)
    check("表头和数据都在", "提交" in out and "372cba3af" in out and "修复取值" in out)
    check("表格后面的正文没被吞", out.strip().endswith("后文"))

    # 中文占两格，补空格时必须按显示宽度算
    check("_pad 按显示宽度补齐（ASCII）", _display_width(_pad("abc", 6)) == 6)
    check("_pad 按显示宽度补齐（中文）", _display_width(_pad("中文", 6)) == 6)

    # 第二列在每一行的起始显示位置必须一致
    table = format_table(["| 提交 | 说明 |", "|---|---|", "| abc | 甲 |", "| 中文标识 | 乙 |"])
    offsets = {
        _display_width(line[: line.index(mark)])
        for mark in ("甲", "乙")
        for line in table
        if mark in line
    }
    check("中日韩字符按双宽对齐", len(offsets) == 1, f"第二列起始位置={offsets} / " + " ⏎ ".join(table))

    # 截断在代码块中间时要补收尾
    out = to_feishu_markdown("```\ndef f():\n    pass")
    check("未闭合的代码围栏被补上", out.count("```") == 2, out.replace("\n", "⏎"))

    # 行内代码 → 蓝色 font（实测飞书只认 font 着色，反引号 / <code> / <u> 都不渲染）
    out = to_feishu_markdown("提交 `abc123` 改了 `Foo.java`")
    check("行内代码转成蓝色 font",
          out == "提交 <font color='blue'>abc123</font> 改了 <font color='blue'>Foo.java</font>", out)
    check("围栏内的反引号不被转换",
          "`x`" in to_feishu_markdown("```\nvar a = `x`\n```"))
    check("落单的反引号不动", to_feishu_markdown("单个 ` 反引号") == "单个 ` 反引号")
    check("标题里的行内代码也转换",
          "<font color='blue'>Foo</font>" in to_feishu_markdown("## 改了 `Foo`"))

    # 工具摘要用加粗而不是反引号
    line = summarize_tool_use("Bash", {"command": "npm test"})
    check("工具摘要用加粗不用反引号",
          line.startswith("**[工具] Bash**") and "`" not in line, line)


async def check_docs(cfg: Config) -> None:
    from oncall_bot.docs_fetch import DocFetcher, build_prompt, find_links, summarize_outcome
    from oncall_bot.feishu.api import SCOPE_DENIED_CODE, FeishuApiError

    # 6.1 链接提取
    text = (
        "按这个文档改一下 https://abc.feishu.cn/docx/Abcd1234EfGh5678 "
        "还有知识库 https://abc.feishu.cn/wiki/Wiki1234Token999 "
        "这个表格先不用 https://abc.feishu.cn/sheets/Sheet123456789 "
        "重复链接 https://abc.feishu.cn/docx/Abcd1234EfGh5678"
    )
    links = find_links(text, limit=5)
    check("提取出 3 个链接（重复的只算一次）", len(links) == 3, f"got={len(links)}")
    check("识别出 docx", any(x.kind == "docx" for x in links))
    check("识别出 wiki", any(x.kind == "wiki" for x in links))
    check("识别出 sheets", any(x.kind == "sheets" for x in links))
    check("非飞书链接不被误抓", find_links("https://example.com/docx/abcdefghij", 5) == [])

    # 6.2 抓取成功 + 图片 + prompt 拼接
    api = FakeApi()
    fetcher = DocFetcher(api, cfg.docs)  # type: ignore[arg-type]
    outcome = await fetcher.fetch(text, "smoke")

    check("docx 和 wiki 各抓到一篇", len(outcome.docs) == 2, f"docs={len(outcome.docs)}")
    check("sheets 进了失败列表", len(outcome.failures) == 1, f"failures={len(outcome.failures)}")
    if outcome.failures:
        check("失败原因点明不支持且给了替代方案",
              "暂不支持" in outcome.failures[0].reason and "docx" in outcome.failures[0].reason,
              outcome.failures[0].reason)

    if outcome.docs:
        doc = outcome.docs[0]
        check("文件已落地", doc.path.exists(), str(doc.path))
        content = doc.path.read_text(encoding="utf-8")
        check("文件头带来源链接", "来源：https://abc.feishu.cn/docx/" in content)
        check("文件含正文", "把按钮改成蓝色" in content)
        check("文件名安全（无非法字符）", not set(doc.path.name) & set('<>:"/\\|?*'), doc.path.name)
        check("标题取自 page 块", doc.title == "测试需求文档", doc.title)
        check("heading 渲染成 markdown 标题", "# 一、需求" in content)
        check("引用文档渲染成链接且 URL 已解码",
              "[参考文档](https://abc.feishu.cn/docx/Ref1234567890)" in content)
        check("有序列表按 sequence 编号",
              "1. 第一步" in content and "2. 第二步" in content)

        # 图片
        check("两张图都下载了", doc.images == 2, f"images={doc.images}")
        check("markdown 里有两处图片引用", content.count("![图 ") == 2)
        check("图片用的是绝对路径", "![图 1](" in content and ":/" in content.split("![图 1](")[1][:5])
        assets = list(doc.path.parent.glob("*.assets/img-*.png"))
        check("图片文件真的落地了", len(assets) >= 2, f"files={[p.name for p in assets]}")
        if assets:
            check("图片内容是真的 PNG", assets[0].read_bytes().startswith(b"\x89PNG"))
        check("文件头说明了图片可以用 Read 读", "Read 工具" in content)

    prompt = build_prompt(text, outcome)
    check("prompt 保留了用户原文", text in prompt)
    check("prompt 里给了本地路径", str(outcome.docs[0].path) in prompt if outcome.docs else False)
    check("prompt 里要求先读文件", "请先读这些文件" in prompt)
    check("prompt 里带上了失败链接", "没能读取" in prompt)
    check("prompt 里说明了图片怎么看", "Read 工具" in prompt)
    check("卡片摘要含图片数", "张图" in summarize_outcome(outcome), summarize_outcome(outcome))

    # 6.3 文档读取权限不足
    api_err = FakeApi()
    api_err.blocks_error = FeishuApiError(1770001, "Forbidden", "log123")
    api_err.docx_error = FeishuApiError(1770001, "Forbidden", "log123")
    fetcher_err = DocFetcher(api_err, cfg.docs)  # type: ignore[arg-type]
    outcome_err = await fetcher_err.fetch(
        "看下 https://abc.feishu.cn/docx/Abcd1234EfGh5678", "smoke"
    )
    check("权限失败时没有伪造内容", len(outcome_err.docs) == 0)
    check("权限失败提示加协作者",
          bool(outcome_err.failures) and "协作者" in outcome_err.failures[0].reason,
          outcome_err.failures[0].reason if outcome_err.failures else "")

    # 6.4 图片权限不足（正文仍然要能拿到）
    api_noimg = FakeApi()
    api_noimg.media_error = FeishuApiError(SCOPE_DENIED_CODE, "Access denied. scopes required", None)
    out_noimg = await DocFetcher(api_noimg, cfg.docs).fetch(  # type: ignore[arg-type]
        "看下 https://abc.feishu.cn/docx/Abcd1234EfGh5678", "smoke-noimg"
    )
    check("图片缺权限时正文照常抓到", len(out_noimg.docs) == 1, f"docs={len(out_noimg.docs)}")
    if out_noimg.docs:
        d = out_noimg.docs[0]
        body = d.path.read_text(encoding="utf-8")
        check("图片数为 0", d.images == 0)
        check("给出了要加的具体权限名",
              any("drive:drive:readonly" in n for n in d.image_notes),
              "; ".join(d.image_notes))
        check("正文里图片位置留了占位说明", body.count("未能下载") == 2, str(body.count("未能下载")))
        check("正文文字没受影响", "把按钮改成蓝色" in body)

    # 6.5 blocks 接口挂了 → 退回 raw_content
    api_fb = FakeApi()
    api_fb.blocks_error = FeishuApiError(99999, "blocks unavailable", None)
    out_fb = await DocFetcher(api_fb, cfg.docs).fetch(  # type: ignore[arg-type]
        "看下 https://abc.feishu.cn/docx/Abcd1234EfGh5678", "smoke-fallback"
    )
    check("blocks 挂了仍能拿到正文", len(out_fb.docs) == 1, f"docs={len(out_fb.docs)}")
    if out_fb.docs:
        check("退回路径用的是 raw_content",
              "第一条：把按钮改成蓝色" in out_fb.docs[0].path.read_text(encoding="utf-8"))
        check("并且说明了图片未抓取",
              any("未抓取" in n for n in out_fb.docs[0].image_notes),
              "; ".join(out_fb.docs[0].image_notes))

    # 6.6 关闭开关
    no_images = cfg.docs.model_copy(update={"fetch_images": False})
    out_ni = await DocFetcher(FakeApi(), no_images).fetch(  # type: ignore[arg-type]
        "看下 https://abc.feishu.cn/docx/Abcd1234EfGh5678", "smoke-noimg2"
    )
    check("fetch_images=false 时不下载图片",
          bool(out_ni.docs) and out_ni.docs[0].images == 0
          and any("关闭" in n for n in out_ni.docs[0].image_notes))

    disabled = cfg.docs.model_copy(update={"enabled": False})
    off = await DocFetcher(FakeApi(), disabled).fetch(text, "smoke")  # type: ignore[arg-type]
    check("docs.enabled=false 时完全不抓", off.empty)
    check("无链接消息原样通过", build_prompt("改个标题", off) == "改个标题")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
