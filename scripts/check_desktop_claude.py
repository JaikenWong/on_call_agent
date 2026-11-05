"""Offline-provider smoke test for the installed real Claude CLI. Uses a temporary HOME."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    read_calls = 0
    edit_calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.startswith("/v1/messages"):
            self.send_error(404)
            return
        names = {t["name"] for t in req.get("tools", [])}
        assert names <= {"mcp__project__project", "mcp__project__edit", "DeferredToolPlaceholder"}, names
        assert "mcp__project__project" in names
        use_edit = "mcp__project__edit" in names
        done = any(
            part.get("type") == "tool_result"
            for message in req.get("messages", [])
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if isinstance(part, dict)
        )
        events = [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": req.get("model", "claude-sonnet-4-6"),
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 8, "output_tokens": 0},
                    }
                },
            ),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "local probe"}}),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
            ),
            ("message_stop", {}),
        ]
        if not done:
            name = "mcp__project__edit" if use_edit else "mcp__project__project"
            values = (
                {"path": "probe.txt", "content": "changed"}
                if use_edit
                else {"action": "read", "path": "probe.txt"}
            )
            if use_edit:
                Handler.edit_calls += 1
            else:
                Handler.read_calls += 1
            events[1] = (
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool_probe", "name": name, "input": {}},
                },
            )
            events[2] = (
                "content_block_delta",
                {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(values)}},
            )
            events[4][1]["delta"]["stop_reason"] = "tool_use"
        if req.get("stream"):
            body = "".join(
                "event: " + name + "\ndata: " + json.dumps(dict(data, type=name)) + "\n\n"
                for name, data in events
            )
            typ = "text/event-stream"
        else:
            body = json.dumps(
                dict(
                    events[0][1]["message"],
                    content=[{"type": "text", "text": "local probe"}],
                    stop_reason="end_turn",
                )
            )
            typ = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())


def main():
    if not shutil.which("claude"):
        raise SystemExit("requires claude on PATH")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / "probe.txt").write_text("read marker", encoding="utf-8")
        env = dict(
            PATH=os.environ["PATH"],
            HOME=tmp,
            ANTHROPIC_API_KEY="test-local",
            ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            TMPDIR=tmp,
        )
        snap = dict(
            path=str(repo),
            sid=str(uuid.uuid4()),
            text="ping",
            dev=repo.stat().st_dev,
            ino=repo.stat().st_ino,
            revision=1,
            generation=1,
            edit=False,
        )
        payload = dict(snapshot=snap, inputs={}, cli=shutil.which("claude"), fresh=True)
        runs = subprocess.run(
            [sys.executable, "-m", "oncall_bot.desktop", "--turn"],
            env=env,
            text=True,
            input=json.dumps(payload) + "\n" + json.dumps(payload) + "\n",
            capture_output=True,
            timeout=90,
        )
        print(runs.returncode, runs.stdout[-800:], runs.stderr[-500:])
        assert runs.returncode == 0
        assert sum("local probe" in line for line in runs.stdout.splitlines()) == 2
        assert Handler.read_calls >= 1 and Handler.edit_calls == 0
        assert (repo / "probe.txt").read_text() == "read marker"
        snap.update(sid=str(uuid.uuid4()), edit=True, text="edit probe.txt")
        payload["snapshot"] = snap
        edit = subprocess.run(
            [sys.executable, "-m", "oncall_bot.desktop", "--turn"],
            env=env,
            text=True,
            input=json.dumps(payload) + "\n",
            capture_output=True,
            timeout=90,
        )
        assert edit.returncode == 0, edit.stdout
        assert Handler.edit_calls == 1
        assert (repo / "probe.txt").read_text() == "changed"
        print("Real CLI read-only tools, warm continuation and explicit edit tools verified.")
    server.shutdown()


if __name__ == "__main__":
    main()
