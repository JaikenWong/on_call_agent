"""给自己私聊发一张「markdown 能力测试卡片」，用实际渲染结果确定飞书支持哪些语法。

对比两种写法：
  A. div + lark_md      —— 当前 cards.py 用的
  B. markdown 组件      —— 卡片 1.0 的独立 markdown 模块

用法：
  python scripts/check_card_markdown.py [--config config.yaml] [--open-id ou_xxx]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody  # noqa: E402

from oncall_bot.config import ConfigError, load_config  # noqa: E402

CODE_SAMPLE = "src/ext/dreame/Foo.java\nsrc/ext/dreame/Bar.java"


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def _note(content: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def build_card() -> dict:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "markdown 能力测试"},
        },
        "elements": [
            _note("A 组：div + lark_md（当前实现用的）"),
            _div("**加粗** / *斜体* / ~~删除线~~ / `行内代码` / [链接](https://open.feishu.cn)"),
            _div("A1 代码围栏：\n```\n" + CODE_SAMPLE + "\n```"),
            _div("A2 标题：\n# 一级标题\n## 二级标题"),
            _div("A3 列表：\n1. 第一项\n2. 第二项\n- 无序项\n  - 嵌套项"),
            _div("A4 引用与分割线：\n> 引用一行\n\n---\n分割线上下"),
            {"tag": "hr"},
            _note("B 组：markdown 组件"),
            _markdown("**加粗** / *斜体* / ~~删除线~~ / `行内代码` / [链接](https://open.feishu.cn)"),
            _markdown("B1 代码围栏：\n```\n" + CODE_SAMPLE + "\n```"),
            _markdown("B2 带语言标记：\n```java\npublic class Foo {}\n```"),
            _markdown("B3 标题：\n# 一级标题\n## 二级标题"),
            _markdown("B4 列表：\n1. 第一项\n2. 第二项\n- 无序项\n  - 嵌套项"),
            _markdown("B5 引用与分割线：\n> 引用一行\n\n---\n分割线上下"),
            _markdown("B6 表格：\n| 列一 | 列二 |\n|---|---|\n| a | b |"),
            {"tag": "hr"},
            _note("请截图回传：看 A1/B1/B2 的代码块有没有渲染成灰底框，以及 A2/B3 标题、B6 表格"),
        ],
    }


SAMPLE_REPLY = """有，最近一条就是：

## 044b1f252（2026-11-02）替代组校验问题修改

改了 2 个文件：

```text
src/ext/dreame/substitutePool2/substituteGroup2/common/service/SubstituteGroup2CreateHandler.java
src/ext/dreame/substitutePool2/util/SubstitutePool2Util.java
```

从 diff 看主要做了三件事：

1. **创建时取产品线**：创建替代组时 `ProductType` 还没写入替代组对象
2. **按产品线隔离校验**：现在只和同产品线的替代组比较
3. **修正漏校验**：改成对物料关联的每个替代组逐个判断

关键改动：

```java
public class SubstituteGroup2CreateHandler {
    private String productType;  // 新增
}
```

| 提交 | 说明 |
|---|---|
| 372cba3af | 修复生效时间差异取值错误 |
| 5eb8bff57 | 追觅扫地机 ECN 影响成品收集逻辑 |

往前的相关提交还有：

- `372cba3af` 修复生效时间差异替代组取值错误问题
- `5eb8bff57` 追觅扫地机 ECN 影响成品收集逻辑替代场场优化

需要看 `044b1f252` 的完整 diff 吗？"""


def build_sample_card() -> dict:
    """走真实渲染管线：to_feishu_markdown + cards.done_card。"""
    from oncall_bot.feishu import cards
    from oncall_bot.runner.render import summarize_tool_use, to_feishu_markdown

    body = "\n\n".join(
        [
            summarize_tool_use("Bash", {"command": "git log --oneline -20"}),
            summarize_tool_use("Read", {"file_path": "src/ext/dreame/SubstitutePool2Util.java"}),
            to_feishu_markdown(SAMPLE_REPLY),
        ]
    )
    return cards.done_card(
        "Windchill",
        body,
        duration_ms=39_000,
        num_turns=6,
        tokens=2_715_134,
        short_session_id="ea6be82b",
    )


INLINE_CANDIDATES = [
    ("C1 反引号（当前）", "创建替代组时 `ProductType` 还没写入，提交 `044b1f252` 改了它"),
    ("C2 灰色 font", "创建替代组时 <font color='grey'>ProductType</font> 还没写入，提交 <font color='grey'>044b1f252</font> 改了它"),
    ("C3 蓝色 font", "创建替代组时 <font color='blue'>ProductType</font> 还没写入，提交 <font color='blue'>044b1f252</font> 改了它"),
    ("C4 红色 font", "创建替代组时 <font color='red'>ProductType</font> 还没写入，提交 <font color='red'>044b1f252</font> 改了它"),
    ("C5 加粗", "创建替代组时 **ProductType** 还没写入，提交 **044b1f252** 改了它"),
    ("C6 下划线标签", "创建替代组时 <u>ProductType</u> 还没写入，提交 <u>044b1f252</u> 改了它"),
    ("C7 code 标签", "创建替代组时 <code>ProductType</code> 还没写入，提交 <code>044b1f252</code> 改了它"),
    ("C8 直角引号", "创建替代组时「ProductType」还没写入，提交「044b1f252」改了它"),
    ("C9 什么都不加", "创建替代组时 ProductType 还没写入，提交 044b1f252 改了它"),
]


def build_inline_card() -> dict:
    elements: list[dict] = [
        _note("行内代码替代写法对照。飞书的 markdown 组件不渲染反引号，下面找一个能渲染的。")
    ]
    for label, content in INLINE_CANDIDATES:
        elements.append(_note(label))
        elements.append(_markdown(content))
    elements.append({"tag": "hr"})
    elements.append(
        _note("请截图回传：哪几行的 ProductType / 044b1f252 有视觉区分（颜色、底色、下划线），"
              "哪几行露出了原始标签")
    )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "行内代码写法测试"},
        },
        "elements": elements,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--open-id", default=None, help="默认取配置里第一个 allowed_open_ids")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="发一张走真实渲染管线的示例卡片（而不是语法对照卡片）",
    )
    parser.add_argument(
        "--inline",
        action="store_true",
        help="发一张「行内代码替代写法」对照卡片",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误]\n{exc}")
        return 2

    open_id = args.open_id or (cfg.policy.allowed_open_ids[0] if cfg.policy.allowed_open_ids else None)
    if not open_id:
        print("配置里 allowed_open_ids 是空的，请用 --open-id ou_xxx 指定接收人。")
        return 2

    client = (
        lark.Client.builder()
        .app_id(cfg.feishu.app_id)
        .app_secret(cfg.feishu.app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )

    if args.inline:
        card = build_inline_card()
    elif args.sample:
        card = build_sample_card()
    else:
        card = build_card()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("interactive")
        .content(json.dumps(card, ensure_ascii=False))
        .build()
    )
    req = CreateMessageRequest.builder().receive_id_type("open_id").request_body(body).build()
    resp = await client.im.v1.message.acreate(req)

    if not resp.success():
        print(f"[FAIL] 发送失败：code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
        if resp.code == 230013:
            print("       这个码一般是机器人和该用户没有会话，先在飞书里主动给机器人发一句话再重试。")
        return 1

    print(f"[OK] 已发送到 {open_id}，message_id={getattr(resp.data, 'message_id', None)}")
    print("     请在飞书里查看并截图回传。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
