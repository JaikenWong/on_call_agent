"""Local maintenance operations. Only generated task directories may be removed."""

import json
import shutil
import time
from pathlib import Path

import yaml


def legacy_preview(filename):
    path = Path(filename)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("配置文件不存在或过大")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("配置格式无效")
    state_path = Path(config.get("state_file", "~/.oncall-bot/state.json")).expanduser()
    if not state_path.is_absolute():
        state_path = path.parent / state_path
    active = None
    if state_path.is_file() and state_path.stat().st_size < 1024 * 1024:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        active = state.get("active") or {}
    return {
        "app": config.get("feishu", {}).get("app_id", ""),
        "cli": config.get("runner", {}).get("cli_path") or "",
        "candidate_path": (active or {}).get("workdir", ""),
        "candidate_sid": (active or {}).get("session_id", ""),
    }


def cleanup(store):
    now = time.time()
    cache = store.root / "tasks"
    # Protect unresolved tasks and any result whose delivery may be retried.
    expired = store.db.execute(
        "SELECT id FROM tasks WHERE created<? AND state IN "
        "('succeeded','cancelled','failed') AND delivery IN ('sent','none','revoked')",
        (now - 86400,),
    ).fetchall()
    for row in expired:
        folder = cache / str(row[0])
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
    with store.db:
        store.db.execute("DELETE FROM images WHERE task_id IS NULL AND ts<?", (now - 600,))
        old = store.db.execute(
            "SELECT id,input_id FROM tasks WHERE created<? AND state IN "
            "('succeeded','cancelled','failed') AND delivery IN ('sent','none','revoked')",
            (now - 7 * 86400,),
        ).fetchall()
        for tid, input_id in old:
            store.db.execute("DELETE FROM images WHERE task_id=?", (tid,))
            store.db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            store.db.execute("UPDATE inputs SET body='' WHERE id=?", (input_id,))
        store.db.execute(
            "UPDATE inputs SET body='' WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE input_id=inputs.id)"
        )
    return len(old)


def diagnostic(store, health):
    counts = store.db.execute("SELECT state,count(*) AS total FROM tasks GROUP BY state")
    return {
        "version": 1,
        "storage": "SQLite",
        "engine": health,
        "paired": bool(store.get("owner")),
        "tasks": {r["state"]: r["total"] for r in counts},
    }
