"""Local control plane. All state transitions run on this asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

from claude_agent_sdk import get_session_info, list_sessions

from ..config import DocsConfig
from ..docs_fetch import DocFetcher
from ..feishu import cards
from ..feishu.parser import UnsupportedMessage, parse_message
from ..feishu.ws import start_ws_thread
from .maintenance import cleanup, diagnostic, legacy_preview
from .process import WarmSlot, terminate
from .resources import DesktopApi, ImageCache
from .runtime import data_root, instance_lock, launch_args
from .store import Store

MAX_FRAME = 256 * 1024
PROGRESS_INTERVAL = 2.0


def alive(group):
    if not group:
        return False
    if os.name == "nt":
        from .windows import alive as win_alive

        return win_alive(group)
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


class Engine:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.api = None
        self.bot = ""
        self.health = "未连接"
        self.challenge = ""
        self.candidate = None
        self.expiry = 0
        self.errors = 0
        self.process = None
        self.active_task_id = None
        self.warm = WarmSlot(store)
        self.images = ImageCache(store)
        self.images.prune()
        self.stop_since = None
        self.closing = False

    async def connect(self):
        import keyring

        app = self.store.get("app")
        if not app or self.api:
            return
        secret = await asyncio.to_thread(keyring.get_password, "OnCallAgent.Feishu", app)
        if not secret:
            raise ValueError("请设置飞书凭据")
        api = DesktopApi(app, secret)
        self.bot = await asyncio.wait_for(api.get_bot_open_id(), 10)
        start_ws_thread(
            app,
            secret,
            asyncio.get_running_loop(),
            self,
            durable=True,
            on_fatal=lambda _: setattr(self, "health", "长连接结束，请重启"),
        )
        self.api = api
        self.health = "应用身份已验证，等待飞书事件"

    async def on_message(self, event):
        header = event.header
        tenant = header.tenant_key
        sender = event.event.sender
        if (
            not tenant
            or header.app_id != self.store.get("app")
            or sender.sender_type != "user"
            or event.event.message.chat_type != "p2p"
        ):
            return
        try:
            msg = parse_message(event, self.bot)
        except UnsupportedMessage:
            return
        if msg.thread_id or not all((msg.sender_open_id, msg.chat_id, msg.message_id)):
            return
        route = [header.app_id, tenant, msg.sender_open_id, msg.chat_id, ""]
        self.health = "已收到飞书事件"
        if msg.text.startswith("/pair "):
            if self.store.get("owner") or not self.challenge or time.monotonic() > self.expiry:
                return
            if secrets.compare_digest(msg.text[6:].strip().encode(), self.challenge.encode()):
                self.candidate = route
                self.challenge = ""
            else:
                self.errors += 1
                if self.errors >= 5:
                    self.challenge = ""
            return
        if route == self.store.get("owner") and msg.text.split()[:1] in (["/new"], ["/use"]):
            await self.warm.close()
        if (
            route == self.store.get("owner")
            and msg.text.split()[:1] == ["/new"]
            and self.active_task_id is not None
        ):
            with self.db:
                # Use the same durable command response path without changing a binding.
                self.store.accept(route, msg.message_id, "/busy", [])
            return
        self.store.accept(route, msg.message_id, msg.text, msg.image_keys)
        if msg.image_keys and self.api and route == self.store.get("owner"):
            self.images.prefetch(self.api, msg.message_id, route)

    def on_card_action(self, event):
        pass

    def snapshot(self):
        return {
            "health": self.health,
            "owner": self.store.get("owner"),
            "candidate": self.candidate,
            "sid": self.store.get("sid"),
            "paused": self.store.get("paused", False),
            "revision": self.store.get("revision", 0),
            "app": self.store.get("app", ""),
            "cli": self.store.get("cli", ""),
            "docs": self.store.get("docs", False),
            "sessions": [
                dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY rowid DESC LIMIT 50")
            ],
            "tasks": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,sid,state,delivery,created,substr(result,1,120) "
                    "AS summary FROM tasks ORDER BY id DESC LIMIT 80"
                )
            ],
        }

    async def command(self, op, p):
        s = self.store
        guarded = {
            "session.acquire",
            "session.release",
            "session.import",
            "session.new",
            "session.review",
            "project.revoke",
            "project.set_policy",
            "setup.save",
        }
        if op in guarded:
            self.require_idle()
        expected_revision = p.get("revision", s.get("revision", 0))
        if op in {
            "session.acquire",
            "session.release",
            "session.import",
            "session.new",
            "project.revoke",
            "project.set_policy",
            "pair.revoke",
            "setup.save",
            "app.shutdown",
        }:
            await self.warm.close()
        if op == "setup.check":
            cli = p.get("cli") or s.get("cli") or shutil.which("claude")
            if not cli or not Path(cli).is_absolute() or not os.access(cli, os.X_OK):
                raise ValueError("CLI 不可执行")
            probe = await asyncio.create_subprocess_exec(
                cli, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            try:
                output, _ = await asyncio.wait_for(probe.communicate(), 5)
            finally:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()
            if probe.returncode:
                raise ValueError("CLI 自检失败")
            return output.decode(errors="replace")[:120]
        if op == "setup.import":
            return legacy_preview(p["path"])
        if op == "diagnostic.get":
            return diagnostic(s, self.health)
        if op == "storage.clean":
            count = cleanup(s)
            self.images.prune()
            return count
        if op == "app.snapshot":
            return self.snapshot()
        if "revision" in p and p["revision"] != s.get("revision", 0):
            raise ValueError("STALE_VERSION：请刷新")
        if op == "setup.save":
            import keyring

            app = p["app"].strip()
            cli = Path(p["cli"]).expanduser().resolve(strict=True)
            if not cli.is_file() or not os.access(cli, os.X_OK):
                raise ValueError("请选择可执行的 Claude CLI")
            changed = bool(s.get("app") and app != s.get("app"))
            if changed and s.get("owner"):
                raise ValueError("请先解绑并退出；使用新的应用需重启引擎")
            if p.get("secret"):
                await asyncio.to_thread(keyring.set_password, "OnCallAgent.Feishu", app, p["secret"])
            with self.db:
                s.set("app", app)
                s.set("cli", str(cli))
                s.set("docs", bool(p.get("docs")))
            if self.api and (changed or p.get("secret")):
                self.closing = True  # QProcess supervisor starts an engine with the new credential.
                return "连接配置已更换，引擎正在重启"
            await self.connect()
            return "连接配置已保存"
        if op == "pair.begin":
            if s.get("owner"):
                raise ValueError("请先解绑")
            self.challenge = secrets.token_urlsafe(16)
            self.expiry = time.monotonic() + 300
            self.candidate = None
            self.errors = 0
            return "/pair " + self.challenge
        if op == "pair.confirm":
            if not self.candidate or self.candidate != p.get("candidate") or time.monotonic() > self.expiry:
                raise ValueError("配对已过期或身份变化")
            s.set("owner", self.candidate)
            self.candidate = None
        elif op == "pair.revoke":
            for job in self.db.execute(
                "SELECT id FROM tasks WHERE state IN ('running','queued','held','preparing')"
            ):
                s.cancel(job[0])
            s.set("owner", None)
            s.set("generation", s.get("generation", 0) + 1)
            s.release()
            self.db.execute("UPDATE replies SET status='revoked' WHERE status='pending'")
            self.db.execute("UPDATE tasks SET delivery='revoked' WHERE delivery='pending'")
            self.db.execute("UPDATE sessions SET revoked=1")
            self.candidate = None
            self.challenge = ""
        elif op == "session.discover":
            infos = await asyncio.to_thread(list_sessions, str(Path(p["path"]).resolve(strict=True)), 50)
            return [
                {"sid": i.session_id, "title": i.custom_title or i.summary or i.session_id} for i in infos
            ]
        elif op == "session.new" or op == "session.import":
            path = str(Path(p["path"]).resolve(strict=True))
            sid = p.get("sid") if op == "session.import" else None
            if op == "session.import":
                info = await asyncio.to_thread(get_session_info, sid, path)
                if not info or not info.cwd or Path(info.cwd).resolve() != Path(path):
                    raise ValueError("历史与目录不匹配")
            if s.get("revision", 0) != expected_revision:
                raise ValueError("STALE_VERSION：接管期间绑定已变化")
            self.require_idle()
            return s.register(path, sid, p.get("title"), bool(p.get("edit")))
        elif op == "session.acquire":
            # Regrant is local and explicit. A failed acquisition rolls the grant back.
            self.db.execute("UPDATE sessions SET revoked=0 WHERE sid=?", (p["sid"],))
            s.bind(p["sid"])
        elif op == "session.release":
            s.idle()
            s.release()
        elif op == "session.review":
            if any(alive(r[0]) for r in self.db.execute("SELECT pid FROM tasks WHERE sid=?", (p["sid"],))):
                raise ValueError("仍有受管进程组，请先核对并结束进程")
            session = s.session(p["sid"])
            found = await asyncio.to_thread(get_session_info, p["sid"], session["path"])
            self.require_idle()
            if s.get("revision", 0) != expected_revision:
                raise ValueError("STALE_VERSION：绑定已变化")
            self.db.execute(
                "UPDATE sessions SET review=0,fresh=? WHERE sid=?", (session["fresh"] and not found, p["sid"])
            )
        elif op == "project.set_policy":
            self.require_idle()
            target = s.session(p["sid"])
            s.check_dir(target)
            self.db.execute("UPDATE sessions SET edit=? WHERE path=?", (bool(p["edit"]), target["path"]))
        elif op == "project.revoke":
            s.idle()
            target = s.session(p["sid"])
            if s.get("sid") and s.session(s.get("sid"))["path"] == target["path"]:
                s.release()
            self.db.execute("UPDATE sessions SET revoked=1 WHERE path=?", (target["path"],))
        elif op == "app.pause" or op == "app.resume":
            s.set("paused", op == "app.pause")
        elif op == "task.cancel":
            s.cancel(p["task_no"])
        elif op == "task.get":
            row = dict(s.task(p["task_no"]))
            row["result"] = row["result"][p.get("offset", 0) : p.get("offset", 0) + 20000]
            row["snapshot"] = json.loads(row["snapshot"])
            return row
        elif op == "task.resume_held":
            job = s.task(p["task_no"])
            snap = json.loads(job["snapshot"])
            if job["state"] != "held" or not self.valid(snap, s.get("revision")):
                raise ValueError("请先接管原会话并检查身份和目录")
            self.db.execute(
                "UPDATE tasks SET state='queued', resumed_rev=? WHERE id=?", (s.get("revision"), job["id"])
            )
        elif op == "delivery.retry":
            job = s.task(p["task_no"])
            if not job["card"] or job["delivery"] not in ("pending", "error"):
                raise ValueError("没有可补发的结果")
            self.db.execute(
                "UPDATE tasks SET delivery='pending',retries=0,retry_at=0 WHERE id=?", (job["id"],)
            )
        elif op == "app.shutdown":
            self.closing = True
        else:
            raise ValueError("未知操作")
        return "完成"

    def require_idle(self):
        if self.active_task_id is not None:
            raise ValueError("BUSY：任务正在执行或收尾")
        self.store.idle()

    def valid(self, snap, resumed_rev=None):
        s = self.store
        if (
            s.get("sid") != snap["sid"]
            or s.get("owner") != snap["route"]
            or s.get("generation", 0) != snap["generation"]
            or s.get("revision") != (resumed_rev or snap["revision"])
            or s.session(snap["sid"])["revoked"]
            or s.session(snap["sid"])["edit"] != snap["edit"]
            or s.session(snap["sid"])["review"]
        ):
            return False
        s.check_dir(snap)
        return True

    async def files(self, job):
        folder = str(job["id"])
        base = self.store.root / "tasks"
        cache_bytes = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
        if cache_bytes >= 1024**3:
            raise ValueError("本地缓存已满，请清理已完成任务")
        inputs = await self.images.collect(self.api, job["id"], base / folder / "inputs")
        if self.store.get("docs"):
            docs = DocFetcher(self.api, DocsConfig(inbox_dir=base, fetch_images=False))
            outcome = await docs.fetch(json.loads(job["snapshot"])["text"], folder)
            if outcome.failures:
                raise ValueError("文档正文获取失败")
            for item in outcome.docs:
                inputs[item.path.name] = str(item.path)
        return inputs

    async def execute(self, job):
        self.active_task_id = job["id"]
        try:
            await self._execute(job)
        finally:
            self.active_task_id = None

    async def _execute(self, job):
        tid = job["id"]
        snap = json.loads(job["snapshot"])
        with self.db:
            self.db.execute("UPDATE tasks SET state='preparing' WHERE id=?", (tid,))
        try:
            if not self.valid(snap, job["resumed_rev"]):
                raise ValueError("授权已变化")
            inputs = await asyncio.wait_for(self.files(job), 30)
            if self.store.task(tid)["state"] != "preparing":
                return
            card = await asyncio.wait_for(
                self.api.reply_card(snap["mid"], cards.running_card(snap["title"], f"执行 #{tid}")), 15
            )
            if not card:
                raise ValueError("首张卡片投递未确认")
            with self.db:
                self.db.execute("UPDATE tasks SET card=? WHERE id=?", (card, tid))
                if self.store.task(tid)["state"] == "cancelled":
                    self.store.finish(tid, "cancelled", "已取消")
                    return
            fresh = self.store.session(snap["sid"])["fresh"]
            if not fresh and not await asyncio.to_thread(get_session_info, snap["sid"], snap["path"]):
                raise ValueError("历史不存在，请重新接管")
            last_patch = time.monotonic()
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in (
                    "HOME",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "USERPROFILE",
                    "LOCALAPPDATA",
                    "APPDATA",
                    "TEMP",
                    "TMP",
                    "PATHEXT",
                    "SYSTEMDRIVE",
                    "CLAUDE_CODE_GIT_BASH_PATH",
                    "USER",
                    "PATH",
                    "LANG",
                    "TMPDIR",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                )
            }
            cli = self.store.get("cli")
            key = None if inputs else (snap["sid"], snap["revision"], snap["edit"], cli, snap["generation"])
            proc = await self.warm.take(key)
            if proc is None:
                proc = await asyncio.create_subprocess_exec(
                    *launch_args("turn"),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    start_new_session=os.name != "nt",
                    limit=2 * 1024 * 1024,
                )
            self.process = proc
            if os.name == "nt" and not getattr(proc, "windows_job", None):
                from .windows import attach_job

                proc.windows_job = attach_job(proc.pid)
            if self.store.task(tid)["state"] != "preparing" or not self.valid(snap, job["resumed_rev"]):
                return
            with self.db:
                self.db.execute("UPDATE tasks SET state='running',pid=? WHERE id=?", (proc.pid, tid))
            payload = dict(snapshot=snap, inputs=inputs, cli=self.store.get("cli"), fresh=bool(fresh))
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            await proc.stdin.drain()
            async with asyncio.timeout(1800):
                while line := await asyncio.wait_for(proc.stdout.readline(), 300):
                    frame = json.loads(line)
                    if frame.get("progress") and time.monotonic() - last_patch >= PROGRESS_INTERVAL:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                self.api.patch_card(
                                    card,
                                    cards.running_card(
                                        snap["title"], f"任务 #{tid} 正在执行 · {frame['progress']}"
                                    ),
                                ),
                                10,
                            )
                        last_patch = time.monotonic()
                    if frame.get("state") in ("succeeded", "failed", "cancelled"):
                        reuse = (
                            key
                            and frame.get("reusable")
                            and frame["state"] == "succeeded"
                            and self.store.task(tid)["state"] == "running"
                            and not self.closing
                        )
                        with self.db:
                            self.store.finish(tid, frame["state"], frame["result"])
                            self.db.execute("UPDATE sessions SET fresh=0 WHERE sid=?", (snap["sid"],))
                        if reuse:
                            self.warm.park(proc, key)
                            self.process = None
                        return
            raise RuntimeError("执行器未返回终态")
        except (Exception, asyncio.CancelledError) as exc:
            state = self.store.task(tid)["state"]
            if state != "cancelled":
                uncertain = state in ("running", "stopping")
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                site = f"{Path(frame.filename).name}:{frame.lineno}"
                cause = f"{type(exc).__name__} ({site}, code={getattr(exc, 'winerror', None)})"
                with self.db:
                    self.store.finish(
                        tid,
                        "unknown" if uncertain else "failed",
                        "执行中断，请核对历史和文件" if uncertain else f"准备失败：{cause}",
                    )
                    if not uncertain and not self.store.task(tid)["card"]:
                        self.db.execute(
                            "UPDATE replies SET status='pending',body=? WHERE id=?",
                            (f"任务 #{tid} 准备失败，请查看桌面", job["input_id"]),
                        )
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            proc = self.process
            if proc:
                # The process group is created by this engine, never by a user's terminal.
                await terminate(proc)
                if not alive(proc.pid):
                    with self.db:
                        self.db.execute("UPDATE tasks SET pid=NULL WHERE pid=?", (proc.pid,))
                self.process = None

    async def worker(self):
        while True:
            row = self.db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row and self.api:
                await self.execute(row)
            else:
                await asyncio.sleep(0.2)

    async def deliver_one(self):
        s = self.store
        r = self.db.execute("SELECT * FROM replies WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if r:
            ok = json.loads(r["route"]) == s.get("owner") and r["generation"] == s.get("generation", 0)
            with self.db:
                self.db.execute(
                    "UPDATE replies SET status=? WHERE id=?", ("sending" if ok else "revoked", r["id"])
                )
            if ok:
                mid = await asyncio.wait_for(self.api.reply_text(r["mid"], r["body"]), 15)
                with self.db:
                    self.db.execute(
                        "UPDATE replies SET status=? WHERE id=?", ("sent" if mid else "unknown", r["id"])
                    )
        job = self.db.execute(
            "SELECT * FROM tasks WHERE delivery='pending' AND retry_at<=? ORDER BY id LIMIT 1", (time.time(),)
        ).fetchone()
        if not job:
            await self.deliver_text_part()
            return
        snap = json.loads(job["snapshot"])
        status = "revoked"
        if snap["route"] == s.get("owner") and snap["generation"] == s.get("generation", 0):
            body = job["result"][:5000]
            if job["state"] == "succeeded":
                card = cards.done_card(
                    snap["title"],
                    body,
                    duration_ms=None,
                    num_turns=None,
                    tokens=None,
                    short_session_id=job["sid"][:8],
                )
            elif job["state"] == "unknown":
                card = cards.stale_card(body)
            else:
                card = cards.failed_card(snap["title"], body)
            try:
                ok = await asyncio.wait_for(self.api.patch_card(job["card"], card), 15)
            except (OSError, TimeoutError):
                ok = False
            status = "sent" if ok else ("error" if job["retries"] >= 3 else "pending")
        with self.db:
            self.db.execute(
                "UPDATE tasks SET delivery=?,retries=retries+1,retry_at=? WHERE id=?",
                (status, time.time() + min(60, 2 ** (job["retries"] + 1)), job["id"]),
            )

    async def deliver_text_part(self):
        row = self.db.execute(
            "SELECT deliveries.*, tasks.snapshot FROM deliveries JOIN tasks "
            "ON tasks.id=deliveries.task_id WHERE deliveries.status='pending' "
            "ORDER BY deliveries.task_id,part LIMIT 1"
        ).fetchone()
        if not row:
            return
        snap = json.loads(row["snapshot"])
        allowed = snap["route"] == self.store.get("owner") and snap["generation"] == self.store.get(
            "generation", 0
        )
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status=? WHERE task_id=? AND part=?",
                ("sending" if allowed else "revoked", row["task_id"], row["part"]),
            )
        if not allowed:
            return
        try:
            mid = await asyncio.wait_for(self.api.reply_text(snap["mid"], row["body"]), 15)
        except Exception:
            mid = None
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status=? WHERE task_id=? AND part=?",
                ("sent" if mid else "unknown", row["task_id"], row["part"]),
            )

    async def monitor(self):
        while True:
            if self.warm.process and time.monotonic() - self.warm.since >= 900:
                await self.warm.close()
            if self.process and self.db.execute("SELECT 1 FROM tasks WHERE state='stopping'").fetchone():
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "nt":
                        await terminate(self.process)
                    elif self.stop_since is None:
                        self.stop_since = time.monotonic()
                        os.kill(self.process.pid, signal.SIGUSR1)
                    elif time.monotonic() - self.stop_since >= 10:
                        os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.stop_since = None
            await asyncio.sleep(0.2)

    async def service(self):
        while True:
            if self.api:
                try:
                    await self.deliver_one()
                except Exception:
                    self.health = "飞书投递失败，请查看任务"
            await asyncio.sleep(0.25)


async def serve():
    os.umask(0o077)
    with instance_lock():
        store = Store(data_root())
        store.recover()
        with store.db:
            # An idle host can briefly survive an abrupt parent exit. Never hand
            # that session back while its old process group may still be writing.
            for r in store.db.execute("SELECT DISTINCT sid,pid FROM tasks WHERE pid IS NOT NULL").fetchall():
                if alive(r["pid"]):
                    store.db.execute("UPDATE sessions SET review=1 WHERE sid=?", (r["sid"],))
                else:
                    store.db.execute("UPDATE tasks SET pid=NULL WHERE pid=?", (r["pid"],))
        cleanup(store)
        engine = Engine(store)
        jobs = [
            asyncio.create_task(engine.worker()),
            asyncio.create_task(engine.service()),
            asyncio.create_task(engine.monitor()),
        ]
        try:
            try:
                await engine.connect()
            except Exception:
                engine.health = "连接失败，请检查凭据与网络"
            print(json.dumps({"v": 1, "type": "hello", "pid": os.getpid()}), flush=True)
            while not engine.closing:
                raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME + 1)
                if not raw or len(raw) > MAX_FRAME:
                    break
                req = {}
                try:
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        raise ValueError("无效请求")
                    req = frame
                    if not isinstance(req.get("id"), str) or not req["id"]:
                        raise ValueError("缺少请求 ID")
                    if req.get("v") != 1:
                        raise ValueError("协议版本不兼容")
                    op, p = req["op"], req.get("params", {})
                    durable = op not in (
                        "app.snapshot",
                        "task.get",
                        "setup.check",
                        "setup.import",
                        "diagnostic.get",
                        "session.discover",
                        "pair.begin",
                        "setup.save",
                        "app.shutdown",
                    )
                    fp = hashlib.sha256(json.dumps([op, p], sort_keys=True).encode()).hexdigest()
                    prior = (
                        store.db.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone()
                        if durable
                        else None
                    )
                    if prior:
                        if prior["fingerprint"] != fp:
                            raise ValueError("ID_CONFLICT")
                        data = json.loads(prior["result"])
                    else:
                        with store.db:
                            data = await engine.command(op, p)
                            if durable:
                                store.db.execute(
                                    "INSERT INTO requests VALUES(?,?,?)", (req["id"], fp, json.dumps(data))
                                )
                    response = {"ok": True, "data": data}
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    }
                print(
                    json.dumps(dict(response, v=1, type="response", id=req.get("id")), ensure_ascii=False),
                    flush=True,
                )
        finally:
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await engine.warm.close()
            await engine.images.close()
            store.recover()
            store.db.close()
