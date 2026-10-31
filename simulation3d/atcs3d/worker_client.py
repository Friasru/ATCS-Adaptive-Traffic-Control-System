"""Async handle on one `sim_worker` subprocess (one SUMO instance + one controller)."""
import asyncio
import json
import os
import signal
import sys

from . import paths


class WorkerError(RuntimeError):
    pass


class WorkerClient:
    def __init__(self, tag):
        self.tag = tag
        self.proc = None
        self._lock = asyncio.Lock()
        self._log = None

    async def spawn(self):
        out_dir = os.path.join(paths.OUTPUT_DIR, self.tag)
        os.makedirs(out_dir, exist_ok=True)
        self._log = open(os.path.join(out_dir, "worker.log"), "ab")
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "atcs3d.sim_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self._log,
            cwd=paths.ROOT,
            start_new_session=True,  # own process group, so we can take sumo down with it
            limit=2**24,
        )
        return self

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def request(self, msg, timeout=60.0):
        """Send one command, wait for its single reply."""
        if not self.alive:
            raise WorkerError("worker is not running")
        async with self._lock:
            self.proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            except asyncio.TimeoutError:
                await self.kill()
                raise WorkerError(f"worker did not answer {msg.get('cmd')!r} within {timeout}s")
            if not line:
                raise WorkerError("worker exited unexpectedly (see worker.log)")
            reply = json.loads(line)
            if reply.get("type") == "error":
                raise WorkerError(reply.get("message", "worker error"))
            return reply

    async def stop(self):
        """Polite shutdown: SUMO is closed (tripinfo flushed) and metrics of what completed are returned."""
        result = None
        if self.alive:
            try:
                result = await self.request({"cmd": "stop"}, timeout=15.0)
            except WorkerError:
                pass
        await self.kill()
        return result

    async def kill(self):
        """Make sure neither the worker nor its sumo child survives."""
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.proc.wait()
        if self._log:
            self._log.close()
            self._log = None
