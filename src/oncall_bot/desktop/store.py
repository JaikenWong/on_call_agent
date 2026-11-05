"""Single-writer desktop journal. No network calls inside a SQLite transaction."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

BUSY = ("queued", "preparing", "running", "stopping")


class Store:
    def __init__(self, root: Path):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(root / "state.sqlite3", timeout=0.5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError("数据库版本不兼容")
        if version == 1:
            # Preserve the installed journal before adding the outbox schema.
            backup = sqlite3.connect(root / "state-before-v2.sqlite3")
            try:
                self.db.backup(backup)
            finally:
                backup.close()
        self.db.executescript("""
        BEGIN;
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          sid TEXT PRIMARY KEY, path TEXT NOT NULL, dev TEXT, ino TEXT,
          title TEXT, fresh INTEGER, edit INTEGER, review INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inputs(
          id INTEGER PRIMARY KEY, app TEXT, tenant TEXT, mid TEXT, body TEXT,
          UNIQUE(app,tenant,mid));
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY, input_id INTEGER UNIQUE REFERENCES inputs, sid TEXT REFERENCES sessions,
          snapshot TEXT NOT NULL, state TEXT NOT NULL, result TEXT DEFAULT '',
          card TEXT, delivery TEXT DEFAULT 'none', retries INTEGER DEFAULT 0,
          retry_at REAL DEFAULT 0, created REAL NOT NULL, pid INTEGER, resumed_rev INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS one_runner ON tasks((1))
          WHERE state IN ('preparing','running','stopping');
        CREATE TABLE IF NOT EXISTS task_events(
          seq INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks ON DELETE CASCADE,
          state TEXT, ts INTEGER DEFAULT(unixepoch()));
        CREATE TRIGGER IF NOT EXISTS task_created AFTER INSERT ON tasks
          BEGIN INSERT INTO task_events(task_id,state) VALUES(NEW.id,NEW.state); END;
        CREATE TRIGGER IF NOT EXISTS task_changed AFTER UPDATE OF state ON tasks WHEN OLD.state!=NEW.state
          BEGIN INSERT INTO task_events(task_id,state) VALUES(NEW.id,NEW.state); END;
        CREATE TABLE IF NOT EXISTS images(
          input_id INTEGER PRIMARY KEY REFERENCES inputs, route TEXT, revision INTEGER,
          keys TEXT, ts REAL, task_id INTEGER REFERENCES tasks);
        CREATE TABLE IF NOT EXISTS replies(
          id INTEGER PRIMARY KEY REFERENCES inputs, route TEXT, mid TEXT, body TEXT,
          status TEXT DEFAULT 'pending', generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, fingerprint TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(
          task_id INTEGER REFERENCES tasks ON DELETE CASCADE,
          part INTEGER, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          PRIMARY KEY(task_id,part));
        PRAGMA user_version=2;
        COMMIT;
        """)
        if "generation" not in {r[1] for r in self.db.execute("PRAGMA table_info(replies)")}:
            with self.db:
                self.db.execute("ALTER TABLE replies ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")

    def get(self, key, default=None):
        row = self.db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def recover(self):
        with self.db:
            # A committed 'running' marker precedes querying Claude. Replaying it
            # after a crash might repeat a file edit, even if no result was saved.
            self.db.execute(
                "UPDATE sessions SET review=1 WHERE sid IN (SELECT sid FROM tasks "
                "WHERE state IN ('running','stopping'))"
            )
            self.db.execute(
                "UPDATE tasks SET state='unknown', result='执行中断，请核对历史和文件', "
                "delivery=CASE WHEN card IS NULL THEN 'none' ELSE 'pending' END "
                "WHERE state IN ('running','stopping')"
            )
            self.db.execute("UPDATE tasks SET state='held' WHERE state IN ('queued','preparing')")
            self.db.execute("UPDATE replies SET status='unknown' WHERE status='sending'")
            self.db.execute("UPDATE deliveries SET status='unknown' WHERE status='sending'")
            self.release()

    def release(self):
        self.set("sid", None)
        self.set("revision", self.get("revision", 0) + 1)
        self.db.execute("DELETE FROM images WHERE task_id IS NULL")

    def idle(self):
        if self.db.execute(
            "SELECT 1 FROM tasks WHERE state IN ('queued','preparing','running','stopping')"
        ).fetchone():
            raise ValueError("BUSY：先取消任务或等待完成")

    def bind(self, sid):
        self.idle()
        s = self.session(sid)
        if s["revoked"] or s["review"]:
            raise ValueError("会话待核对")
        self.check_dir(s)
        self.release()
        self.set("sid", sid)

    def session(self, sid):
        s = self.db.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not s:
            raise ValueError("会话未登记")
        return dict(dict(s), dev=int(s["dev"]), ino=int(s["ino"]))

    @staticmethod
    def check_dir(s):
        st = os.stat(s["path"])
        if (st.st_dev, st.st_ino) != (int(s["dev"]), int(s["ino"])) or not Path(s["path"]).is_dir():
            raise ValueError("项目目录已经变化，请重新登记")

    def register(self, path, sid=None, title=None, edit=False):
        self.idle()
        path = Path(path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("请选择项目目录")
        if path.is_relative_to(self.root) or self.root.is_relative_to(path):
            raise ValueError("项目不能与桌面应用的私有数据目录重叠")
        st = path.stat()
        fresh = sid is None
        sid = sid or str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO sessions(sid,path,dev,ino,title,fresh,edit) VALUES(?,?,?,?,?,?,?)",
            (sid, str(path), str(st.st_dev), str(st.st_ino), title or path.name, fresh, edit),
        )
        self.bind(sid)
        return sid

    def accept(self, route, mid, text, keys):
        """Dedup, bind, claim pending images and enqueue the reply in one commit."""
        with self.db:
            if route != self.get("owner"):
                return
            row = self.db.execute(
                "INSERT INTO inputs(app,tenant,mid,body) VALUES(?,?,?,?) ON CONFLICT DO NOTHING RETURNING id",
                (*route[:2], mid, text),
            ).fetchone()
            if not row:
                return
            i = row[0]
            self.db.execute("SAVEPOINT apply_event")
            try:
                note = self._apply(i, route, mid, text, keys)
            except (ValueError, OSError) as exc:
                self.db.execute("ROLLBACK TO apply_event")
                note = str(exc)
            self.db.execute("RELEASE apply_event")
            self.db.execute(
                "INSERT INTO replies(id,route,mid,body,generation) VALUES(?,?,?,?,?)",
                (i, json.dumps(route), mid, note, self.get("generation", 0)),
            )

    def _apply(self, i, route, mid, text, keys):
        sid = self.get("sid")
        rev = self.get("revision", 0)
        if text.startswith("/"):
            args = text.split()
            if args[0] == "/status":
                return f"会话 {sid or '待接管'}，暂停接单 {self.get('paused', False)}"
            if args[0] == "/sessions":
                rows = self.db.execute(
                    "SELECT title,sid FROM sessions WHERE NOT revoked ORDER BY rowid DESC LIMIT 50"
                ).fetchall()
                self.set("listing", [rev, time.time(), [s[1] for s in rows]])
                return "\n".join(f"{n}. {s[0]} · {s[1]}" for n, s in enumerate(rows, 1)) or "请先在桌面登记"
            if args[0] == "/use" and len(args) == 2:
                target = args[1]
                if target.isdigit():
                    version, ts, ids = self.get("listing", [-1, 0, []])
                    n = int(target)
                    if version != rev or time.time() - ts > 300 or not 1 <= n <= len(ids):
                        raise ValueError("请重新 /sessions")
                    target = ids[n - 1]
                if self.session(target)["revoked"]:
                    raise ValueError("会话未登记")
                if target != sid:
                    raise ValueError("请在桌面确认接管该历史")
                return "已接管"
            if args[0] == "/new" and len(args) <= 2:
                if len(args) == 2:
                    projects = self.db.execute(
                        "SELECT * FROM sessions WHERE NOT revoked AND NOT review AND title=?", (args[1],)
                    ).fetchall()
                    if len({r["path"] for r in projects}) != 1:
                        raise ValueError("项目别名不存在或不唯一")
                    self.check_dir(projects[0])
                    path = projects[0]["path"]
                else:
                    current = self.session(sid)
                    if current["review"] or current["revoked"]:
                        raise ValueError("项目需要桌面重新授权")
                    self.check_dir(current)
                    path = current["path"]
                return "已新建 " + self.register(path)
            if args[0] == "/stop" and len(args) <= 2:
                jobs = [
                    r
                    for r in self.db.execute(
                        "SELECT * FROM tasks WHERE state IN "
                        "('preparing','running','queued','held','stopping') ORDER BY id"
                    )
                    if json.loads(r["snapshot"])["route"] == route
                ]
                if len(args) == 2:
                    jobs = [r for r in jobs if str(r["id"]) == args[1]]
                else:
                    jobs = [r for r in jobs if r["state"] in ("preparing", "running", "stopping")] or jobs
                if len(jobs) != 1:
                    raise ValueError("请指定任务号：" + ", ".join(str(r["id"]) for r in jobs))
                self.cancel(jobs[0]["id"])
                return f"已请求停止 #{jobs[0]['id']}"
            return "命令：/status /sessions /use ID /new [项目] /stop [任务号]"
        if not sid or self.get("paused", False):
            return "未接管会话或已暂停，请查看桌面"
        s = self.session(sid)
        self.check_dir(s)
        if s["revoked"] or s["review"]:
            return "该会话需要核对"
        scope = json.dumps(route)
        self.db.execute("DELETE FROM images WHERE task_id IS NULL AND ts < ?", (time.time() - 600,))
        pending = self.db.execute(
            "SELECT keys FROM images WHERE route=? AND revision=? AND task_id IS NULL", (scope, rev)
        ).fetchall()
        if len(keys) + sum(len(json.loads(r[0])) for r in pending) > 9:
            return "暂存图片上限为 9 张"
        if (
            text
            and self.db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','held')").fetchone()[0]
            >= 5
        ):
            return "等待队列已满，暂存图片已保留"
        if keys:
            self.db.execute(
                "INSERT INTO images VALUES(?,?,?,?,?,NULL)", (i, scope, rev, json.dumps(keys), time.time())
            )
        if not text:
            return "图片已登记，请发需求" if keys else "请输入需求"
        snap = dict(s, route=route, revision=rev, mid=mid, text=text, generation=self.get("generation", 0))
        # Keep display numbers and private directories unique after retention
        # deletes the highest task row. Input IDs alone are not task IDs in v1.
        highest = self.db.execute("SELECT coalesce(max(id),0) FROM tasks").fetchone()[0]
        tid = max(highest, self.get("last_task_no", 0)) + 1
        self.set("last_task_no", tid)
        self.db.execute(
            "INSERT INTO tasks(id,input_id,sid,snapshot,state,created) VALUES(?,?,?,?,'queued',?)",
            (tid, i, sid, json.dumps(snap), time.time()),
        )
        self.db.execute(
            "UPDATE images SET task_id=? WHERE route=? AND revision=? AND task_id IS NULL", (tid, scope, rev)
        )
        return f"已排队 #{tid} · {s['title']}"

    def cancel(self, tid):
        row = self.task(tid)
        if row["state"] in ("queued", "held", "preparing"):
            self.finish(tid, "cancelled", "用户取消")
        elif row["state"] == "running":
            self.db.execute("UPDATE tasks SET state='stopping' WHERE id=?", (tid,))

    def task(self, tid):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        return row

    def finish(self, tid, state, result):
        self.db.execute(
            "UPDATE tasks SET state=?,result=?,delivery=CASE WHEN card IS NULL THEN 'none' "
            "ELSE 'pending' END WHERE id=?",
            (state, result, tid),
        )
        job = self.task(tid)
        chunks = []
        if job["card"] and len(result) > 5000:
            chunks = [result[start : start + 3500] for start in range(0, len(result), 3500)]
        elif not job["card"] and state in ("cancelled", "failed", "unknown"):
            chunks = [f"任务 #{tid} · {state}\n{result}"]
        self.db.executemany(
            "INSERT INTO deliveries(task_id,part,body) VALUES(?,?,?) ON CONFLICT DO NOTHING",
            [(tid, n, body) for n, body in enumerate(chunks)],
        )
        if state == "unknown":
            self.db.execute(
                "UPDATE sessions SET review=1 WHERE sid=(SELECT sid FROM tasks WHERE id=?)", (tid,)
            )
