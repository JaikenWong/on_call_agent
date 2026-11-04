"""Desktop regressions through the durable receive, subprocess and replay boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from oncall_bot.desktop.engine import Engine
from oncall_bot.desktop.maintenance import cleanup, diagnostic
from oncall_bot.desktop.store import Store
from oncall_bot.desktop.turn import project_io

OWNER = ["cli_test", "tenant", "alice", "dm", ""]


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "data")
    engine = Engine(store)
    with store.db:
        store.set("app", OWNER[0])
        store.set("owner", OWNER)
        repo = tmp_path / "repo"
        repo.mkdir()
        store.register(repo)
    yield engine
    store.db.close()


def message(mid, text="", *, chat="p2p", user="alice", keys=None, thread="", app="cli_test"):
    kind = "image" if keys else "text"
    return NS(
        header=NS(app_id=app, tenant_key="tenant"),
        event=NS(
            sender=NS(sender_id=NS(open_id=user), sender_type="user"),
            message=NS(
                chat_type=chat,
                chat_id="dm",
                message_id=mid,
                message_type=kind,
                mentions=[],
                thread_id=thread,
                content=json.dumps({"image_key": keys} if keys else {"text": text}),
            ),
        ),
    )


async def test_identity_image_claim_and_scoped_cancel(app):
    await app.on_message(message("img", keys="key"))
    for event in [
        message("b", "steal", user="bob"),
        message("group", "/sessions", chat="group"),
        message("other", "steal", app="other"),
        message("thread", "steal", thread="t"),
    ]:
        await app.on_message(event)
    await app.on_message(message("a", "inspect"))
    await app.on_message(message("stop-other", "/stop", user="bob"))
    await app.on_message(message("a", "inspect"))
    assert app.db.execute("select count(*) from tasks").fetchone()[0] == 1
    assert app.store.task(1)["state"] == "queued"
    assert app.db.execute("select task_id from images").fetchone()[0] == 1
    await app.on_message(message("stop", "/stop 1"))
    assert app.store.task(1)["state"] == "cancelled"


async def test_full_queue_preserves_pending_and_binding(app):
    for n in range(5):
        await app.on_message(message(str(n), "work"))
    await app.on_message(message("image", keys="x"))
    await app.on_message(message("full", "work"))
    await app.on_message(message("switch", "/new"))
    assert not app.db.execute("select task_id from images").fetchone()[0]
    assert app.db.execute("select count(*) from sessions").fetchone()[0] == 1
    await app.on_message(message("cancel", "/stop 1"))
    await app.on_message(message("accepted", "work"))
    assert app.db.execute("select task_id from images").fetchone()[0] == 6


async def test_challenge_requires_desktop_confirmation(app):
    with app.db:
        await app.command("pair.revoke", {})
    cmd = await app.command("pair.begin", {})
    await app.on_message(message("c", cmd))
    await app.on_message(message("c2", cmd, user="bob"))
    assert app.store.get("owner") is None and app.candidate == OWNER
    with app.db:
        await app.command("pair.confirm", {"candidate": OWNER})
    assert app.store.get("owner") == OWNER


async def test_worker_crash_after_commit_retries_card_only(app, tmp_path, monkeypatch):
    api = NS(
        reply_card=AsyncMock(return_value="card1"),
        reply_text=AsyncMock(return_value="reply"),
        patch_card=AsyncMock(return_value=False),
    )
    app.api = api
    count = tmp_path / "calls"
    child = tmp_path / "fake-turn.py"
    child.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "d=json.loads(sys.stdin.buffer.readline())\n"
        f"with Path({str(count)!r}).open('a') as f: f.write(d['snapshot']['mid']+'\\n')\n"
        "print(json.dumps(dict(state='succeeded',result='finished')))\n"
    )
    monkeypatch.setattr("oncall_bot.desktop.engine.launch_args", lambda _: [sys.executable, str(child)])
    await app.on_message(message("original", "fix"))
    await app.execute(app.store.task(1))
    assert app.store.task(1)["state"] == "succeeded"
    recovered = Store(app.store.root)
    try:
        recovered.recover()
        new = Engine(recovered)
        new.api = api
        await new.deliver_one()
        assert recovered.task(1)["delivery"] == "pending"
        api.patch_card.return_value = True
        with recovered.db:
            await new.command("delivery.retry", {"task_no": 1})
        await new.deliver_one()
        assert recovered.task(1)["delivery"] == "sent"
        assert api.patch_card.await_args.args[0] == "card1"
        assert count.read_text() == "original\n"
    finally:
        recovered.db.close()


def test_crash_freezes_queue_but_never_replays_started(app):
    for i in range(2):
        app.store.accept(OWNER, str(i), "work", [])
    with app.db:
        app.db.execute("update tasks set state='running' where id=1")
    app.store.recover()
    assert [r[0] for r in app.db.execute("select state from tasks order by id")] == ["unknown", "held"]
    assert app.store.get("sid") is None
    with pytest.raises(ValueError, match="待核对"):
        app.store.bind(app.store.task(1)["sid"])


def test_directory_policy_and_atomic_writes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    identity = (root.stat().st_dev, root.stat().st_ino)
    outside = tmp_path / "private"
    outside.write_text("private")
    (root / "redirect").symlink_to(outside)
    os.link(outside, root / "hard")
    for path in ("../private", ".git/config", "redirect", "hard", str(outside)):
        for action in ("read", "write"):
            with pytest.raises((ValueError, OSError)):
                project_io(root, identity, path, action, "changed")
    project_io(root, identity, "safe", "write", "ok")
    assert project_io(root, identity, "safe", "read") == "ok"
    assert outside.read_text() == "private"


def test_cleanup_leaves_unknown_data_and_diagnostics_private(app):
    app.store.accept(OWNER, "1", "private text", [])
    with app.db:
        app.db.execute("UPDATE tasks SET created=0,state='unknown'")
    cleanup(app.store)
    assert app.store.task(1)["snapshot"]
    assert "private text" not in json.dumps(diagnostic(app.store, "ready"))


def test_engine_ipc_single_instance_and_restart(tmp_path):
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        LOCALAPPDATA=str(tmp_path / "local"),
        PYTHONUTF8="1",
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    cmd = [sys.executable, "-m", "oncall_bot.desktop", "--engine"]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True) as first:
        try:
            assert json.loads(first.stdout.readline())["type"] == "hello"
            other = subprocess.run(
                cmd, input="", capture_output=True, text=True, encoding="utf-8", env=env, timeout=30
            )
            assert other.returncode and "已有引擎" in other.stderr

            def call(rid, op, params):
                first.stdin.write(json.dumps(dict(v=1, id=rid, op=op, params=params)) + "\n")
                first.stdin.flush()
                return json.loads(first.stdout.readline())

            repo = tmp_path / "repo"
            repo.mkdir()
            sid = call("new", "session.new", {"path": str(repo)})["data"]
            assert call("new", "session.new", {"path": str(repo)})["data"] == sid
            assert call("new", "session.release", {})["error"] == "ID_CONFLICT"
            assert call("exit", "app.shutdown", {})["ok"]
            first.wait(timeout=10)
        finally:
            first.kill()
