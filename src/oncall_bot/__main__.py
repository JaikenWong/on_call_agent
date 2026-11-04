"""入口：加载配置 → 启动主循环 → 启动飞书长连接线程。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .app import App
from .config import ConfigError, load_config
from .feishu.ws import start_ws_thread
from .logging_setup import setup_logging

log = logging.getLogger("oncall_bot")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="oncall-bot", description="飞书 ↔ Claude Code 会话桥")
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--debug", action="store_true", help="日志级别设为 DEBUG")
    return parser.parse_args(argv)


async def _serve(app: App, app_id: str, app_secret: str) -> int:
    await app.startup()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    exit_code = 0

    def request_stop() -> None:
        log.info("收到退出信号")
        stop.set()

    def _fatal(reason: str, event: asyncio.Event) -> None:
        """长连接不可恢复地断掉：进程留着也收不到消息，交给外部守护进程重启。"""
        nonlocal exit_code
        log.error("致命错误，准备退出：%s", reason)
        exit_code = 1
        event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, AttributeError):
            # Windows 的 Proactor 循环不支持 add_signal_handler，交给 KeyboardInterrupt
            signal.signal(sig, lambda *_: request_stop())

    start_ws_thread(app_id, app_secret, loop, app, on_fatal=lambda reason: _fatal(reason, stop))
    log.info("已就绪。在飞书里 @ 机器人发 /help 看命令。")

    try:
        await stop.wait()
    finally:
        await app.shutdown()
        log.info("已退出")
    return exit_code


def cli(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误]\n{exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir, logging.DEBUG if args.debug else logging.INFO)
    app = App(cfg)
    try:
        code = asyncio.run(_serve(app, cfg.feishu.app_id, cfg.feishu.app_secret))
    except KeyboardInterrupt:
        log.info("Ctrl-C 退出")
        return 0
    except Exception:
        log.exception("启动失败")
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(cli())
