"""Regression cases found by comparing the first release to its design contracts."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from oncall_bot.desktop.engine import Engine
from oncall_bot.desktop.maintenance import cleanup
from oncall_bot.desktop.store import Store

OWNER = ["cli", "tenant", "me", "dm", ""]


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "state"
    project = tmp_path / "repo"
    project.mkdir()
    store = Store(root)
    with store.db:
        store.set("owner", OWNER)
        store.register(project)
    app = Engine(store)
    yield app
    store.db.close()


async def test_release_and_configuration_wait_for_execution_cleanup(engine):
    engine.active_task_id = 12
    with pytest.raises(ValueError, match="BUSY"):
        await engine.command("session.release", {})
    with pytest.raises(ValueError, match="BUSY"):
        await engine.command("setup.save", {"app": "cli", "cli": "/missing"})
    assert engine.store.get("sid")


def test_completed_task_numbers_never_reuse_private_input_directories(engine):
    s = engine.store
    s.accept(OWNER, "first", "inspect", [])
    first = s.db.execute("SELECT id FROM tasks").fetchone()[0]
    with s.db:
        s.finish(first, "succeeded", "old")
        s.db.execute("UPDATE tasks SET created=0")
    cleanup(s)
    s.accept(OWNER, "second", "inspect", [])
    second = s.db.execute("SELECT id FROM tasks").fetchone()[0]
    assert second > first


async def test_long_result_has_durable_complete_delivery(engine):
    s = engine.store
    s.accept(OWNER, "long", "inspect", [])
    with s.db:
        s.db.execute("UPDATE tasks SET card='card'")
        s.finish(1, "succeeded", "完整结果" * 2400)
    engine.api = NS(reply_text=AsyncMock(return_value="reply"), patch_card=AsyncMock(return_value=True))
    for _ in range(10):
        await engine.deliver_one()
    # The queued acknowledgement is separate from the full result chunks.
    outputs = [
        call.args[1] for call in engine.api.reply_text.await_args_list if call.args[1] != "已排队 #1 · repo"
    ]
    assert "".join(outputs) == "完整结果" * 2400
    restored = Store(s.root)
    try:
        restored.recover()
        app = Engine(restored)
        app.api = engine.api
        count = engine.api.reply_text.await_count
        await app.deliver_one()
        assert engine.api.reply_text.await_count == count
    finally:
        restored.db.close()


async def test_late_history_lookup_cannot_override_a_new_binding(engine, monkeypatch):
    s = engine.store
    old = s.get("sid")
    entered, leave = asyncio.Event(), asyncio.Event()

    def get_info(*_):
        return NS(cwd=s.session(old)["path"])

    async def delayed(*args, **kwargs):
        entered.set()
        await leave.wait()
        return get_info()

    monkeypatch.setattr("oncall_bot.desktop.engine.asyncio.to_thread", delayed)
    request = asyncio.create_task(
        engine.command(
            "session.import",
            {"path": s.session(old)["path"], "sid": "external", "revision": s.get("revision")},
        )
    )
    await entered.wait()
    with s.db:
        s.release()
    leave.set()
    with pytest.raises(ValueError, match="STALE_VERSION"):
        await request


async def test_cancelled_queued_task_notifies_its_original_message(engine):
    s = engine.store
    s.accept(OWNER, "queued", "inspect", [])
    with s.db:
        s.db.execute("UPDATE replies SET status='sent'")
        s.cancel(1)
    engine.api = NS(reply_text=AsyncMock(return_value="reply"), patch_card=AsyncMock(return_value=True))
    await engine.deliver_one()
    assert engine.api.reply_text.await_count == 1
    assert engine.api.reply_text.await_args.args[0] == "queued"


async def test_damaged_image_content_never_becomes_model_input(engine):
    s = engine.store
    s.accept(OWNER, "image", "", ["key"])
    row = s.db.execute("SELECT images.*, inputs.mid FROM images JOIN inputs ON input_id=inputs.id").fetchone()
    with pytest.raises(ValueError, match="格式"):
        await engine.images._load(
            NS(download_message_resource=AsyncMock(return_value=b"<html>denied</html>")), row
        )


def test_v1_upgrade_preserves_pairing_history_and_creates_backup(engine):
    s = engine.store
    sid = s.get("sid")
    with s.db:
        s.db.execute("PRAGMA user_version=1")
        # Match the published v1 reply table which had no authorization generation.
        s.db.execute("ALTER TABLE replies DROP COLUMN generation")
    upgraded = Store(s.root)
    try:
        assert upgraded.get("owner") == OWNER
        assert upgraded.session(sid)["path"] == s.session(sid)["path"]
        assert (s.root / "state-before-v2.sqlite3").is_file()
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "generation" in {r[1] for r in upgraded.db.execute("PRAGMA table_info(replies)")}
    finally:
        upgraded.db.close()


async def test_old_reply_not_delivered_after_repairing_same_person(engine):
    s = engine.store
    s.accept(OWNER, "old", "inspect", [])
    with s.db:
        s.set("generation", 1)
    engine.api = NS(reply_text=AsyncMock(return_value="reply"))
    await engine.deliver_one()
    assert engine.api.reply_text.await_count == 0
    assert s.db.execute("SELECT status FROM replies").fetchone()[0] == "revoked"


async def test_held_edit_task_cannot_resume_with_downgraded_permissions(engine):
    s = engine.store
    sid = s.get("sid")
    with s.db:
        await engine.command("project.set_policy", {"sid": sid, "edit": True})
    s.accept(OWNER, "edit", "change file", [])
    s.recover()
    with s.db:
        await engine.command("session.acquire", {"sid": sid})
        await engine.command("project.set_policy", {"sid": sid, "edit": False})
    with pytest.raises(ValueError, match="检查身份"):
        await engine.command("task.resume_held", {"task_no": 1})
    assert s.task(1)["state"] == "held"


async def test_progress_patch_failure_cannot_abort_query(engine, tmp_path, monkeypatch):
    import sys

    child = tmp_path / "progress.py"
    child.write_text(
        "import json,sys\njson.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'progress':'AssistantMessage'}),flush=True)\n"
        "print(json.dumps({'state':'succeeded','result':'done'}),flush=True)\n"
    )
    monkeypatch.setattr("oncall_bot.desktop.engine.launch_args", lambda _: [sys.executable, str(child)])
    monkeypatch.setattr("oncall_bot.desktop.engine.PROGRESS_INTERVAL", 0)
    engine.api = NS(reply_card=AsyncMock(return_value="card"), patch_card=AsyncMock(side_effect=TimeoutError))
    engine.store.accept(OWNER, "message", "inspect", [])
    await engine.execute(engine.store.task(1))
    assert engine.api.patch_card.await_count == 1
    assert engine.store.task(1)["state"] == "succeeded"
    assert engine.active_task_id is None
