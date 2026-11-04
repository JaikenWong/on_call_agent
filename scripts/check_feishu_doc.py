"""用真实凭据验证一条飞书云文档链接能否读到。

只调云文档相关接口，不发任何飞书消息、不启动 Claude。
不打印 app_secret。

用法：
  python scripts/check_feishu_doc.py "https://xxx.feishu.cn/docx/<token>" [--config config.yaml]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oncall_bot.config import ConfigError, load_config  # noqa: E402
from oncall_bot.docs_fetch import DocFetcher, build_prompt, find_links  # noqa: E402
from oncall_bot.feishu.api import FeishuApi, FeishuApiError  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--config", "-c", default="config.yaml")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误]\n{exc}")
        return 2

    print("=== 凭据 ===")
    print(f"  app_id = {cfg.feishu.app_id[:10]}…（长度 {len(cfg.feishu.app_id)}）")
    print(f"  app_secret = <已配置，长度 {len(cfg.feishu.app_secret)}>")

    print("\n=== 1. 正则解析链接 ===")
    links = find_links(args.url, limit=5)
    if not links:
        print("  [FAIL] 正则没有匹配到任何飞书云文档链接")
        print(f"         原始输入：{args.url}")
        return 1
    for link in links:
        print(f"  [OK] kind={link.kind} token={link.token}（{len(link.token)} 位）")

    api = FeishuApi(cfg.feishu.app_id, cfg.feishu.app_secret)
    link = links[0]

    print("\n=== 2. 直接调接口 ===")
    token = link.token
    if link.kind == "wiki":
        try:
            obj_type, obj_token, title = await api.wiki_node(token)
        except FeishuApiError as exc:
            print(f"  [FAIL] wiki_node 失败：code={exc.code} msg={exc.msg} log_id={exc.log_id}")
            return 1
        print(f"  [OK] wiki 节点：obj_type={obj_type} obj_token={obj_token} title={title!r}")
        if obj_type != "docx":
            print(f"  这个节点指向 {obj_type}，当前实现只支持 docx")
            return 1
        token = obj_token

    try:
        content = await api.docx_raw_content(token)
    except FeishuApiError as exc:
        print("  [FAIL] docx_raw_content 失败")
        print(f"         code    = {exc.code}")
        print(f"         msg     = {exc.msg}")
        print(f"         log_id  = {exc.log_id}")
        print("\n  这个 code/msg 就是接下来要用来区分「没权限」和「其他错误」的依据。")
        return 1

    title = await api.docx_title(token)
    print(f"  [OK] 读到内容：{len(content)} 字")
    print(f"  [OK] 标题：{title!r}")
    print("\n--- 正文前 500 字 ---")
    print(content[:500])
    print("--- 正文结束（截断显示）---")

    print("\n=== 3. 走完整 DocFetcher 流程 ===")
    fetcher = DocFetcher(api, cfg.docs)
    outcome = await fetcher.fetch(args.url, "realcheck")
    for doc in outcome.docs:
        print(f"  [OK] 已落地：{doc.path}")
        print(f"       标题={doc.title!r} 字数={doc.chars} 截断={doc.truncated} 图片={doc.images}")
        for note in doc.image_notes:
            print(f"       注意：{note}")
        body = doc.path.read_text(encoding="utf-8")
        print(f"       markdown 图片引用数：{body.count('![图 ')}")
        print(f"       未下载的图片占位数：{body.count('未能下载')}")
        print("\n--- md 正文前 1200 字 ---")
        print(body[:1200])
        print("--- 截断显示 ---")
    for failure in outcome.failures:
        print(f"  [FAIL] {failure.url}\n         {failure.reason}")

    if outcome.docs:
        print("\n=== 4. 交给 Claude 的 prompt（前 800 字）===")
        print(build_prompt(f"按这个文档改一下 {args.url}", outcome)[:800])

    return 0 if outcome.docs else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
