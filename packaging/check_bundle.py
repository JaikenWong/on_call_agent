"""Run the portable folder with temporary local state, without credentials."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def check(folder):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = dict(
            os.environ,
            HOME=tmp,
            USERPROFILE=tmp,
            LOCALAPPDATA=str(root / "Local"),
            QT_QPA_PLATFORM="offscreen",
            PYTHONUTF8="1",
        )
        repo = root / "测试 repo"
        repo.mkdir()
        commands = [
            ("new", "session.new", {"path": str(repo)}),
            ("status", "app.snapshot", {}),
            ("exit", "app.shutdown", {}),
        ]
        request = "".join(json.dumps(dict(v=1, id=i, op=op, params=p)) + "\n" for i, op, p in commands)
        exe = folder / "OnCallEngine.exe" if os.name == "nt" else folder / "OnCallAgent"
        result = subprocess.run(
            [exe, "--engine"],
            input=request,
            encoding="utf-8",
            capture_output=True,
            env=env,
            cwd=tmp,
            timeout=60,
            check=True,
        )
        msgs = [json.loads(line) for line in result.stdout.splitlines()]
        assert msgs[0]["type"] == "hello" and all(m["ok"] for m in msgs[1:]), msgs
        assert msgs[1]["data"] == msgs[2]["data"]["sid"]
        gui = folder / ("OnCallAgent.exe" if os.name == "nt" else "OnCallAgent")
        subprocess.run([gui, "--check-ui"], env=env, cwd=tmp, timeout=60, check=True)
        print("Packaged GUI connected to its engine; engine created and restored its session registry.")


if __name__ == "__main__":
    check(Path(sys.argv[1]).resolve())
