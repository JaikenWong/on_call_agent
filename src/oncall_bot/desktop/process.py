"""Own one task process group, optionally retained between compatible turns."""

import asyncio
import contextlib
import os
import signal
import time


async def terminate(proc):
    if os.name == "nt":
        job = getattr(proc, "windows_job", None)
        if job is not None:
            job.Close()
            proc.windows_job = None
        elif proc.returncode is None:
            proc.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


class WarmSlot:
    def __init__(self, store):
        self.store = store
        self.process = None
        self.key = None
        self.since = 0
        self.lock = asyncio.Lock()

    def park(self, proc, key):
        self.process = proc
        self.key = key
        self.since = time.monotonic()

    async def take(self, key):
        async with self.lock:
            if key and key == self.key and self.process and self.process.returncode is None:
                proc = self.process
                self.process = self.key = None
                return proc
            await self._close()

    async def close(self):
        async with self.lock:
            await self._close()

    async def _close(self):
        if self.process is None:
            return
        proc = self.process
        self.process = self.key = None
        try:
            # EOF allows the host to exit ClaudeSDKClient's async context normally.
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 1)
        except TimeoutError:
            pass
        finally:
            await terminate(proc)
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET pid=NULL WHERE pid=?", (proc.pid,))
