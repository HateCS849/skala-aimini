"""Own exactly one synthetic fixture process. No arbitrary PID, shell, container or app control."""
import asyncio
import os
import socket
import sys
import time
from urllib.parse import urlsplit

import httpx

from .storage import ROOT


class Fixture:
    def __init__(self):
        self.process = None
        self.log = None
        self.lock = asyncio.Lock()

    def owns_url(self, url):
        parts = urlsplit(url)
        return self.process is not None and self.process.returncode is None and parts.hostname in {"127.0.0.1", "localhost"} and parts.port == 8731

    async def start(self):
        for port in (8731, 8732):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"Fixture port {port} occupied; existing process left untouched")
        folder = ROOT / "data" / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.log = (folder / "8731.log").open("a")
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-m", "uvicorn", "examples.resilience_app:app", "--host", "127.0.0.1", "--port", "8731",
            "--timeout-graceful-shutdown", "4", "--no-access-log", cwd=ROOT, env={**os.environ, "LANGSMITH_TRACING": "false"}, stdout=self.log, stderr=self.log)
        try:
            return await self.health()
        except BaseException:
            await self.stop()
            raise

    async def health(self):
        async with httpx.AsyncClient(trust_env=False, timeout=.5) as client:
            for _ in range(100):
                if self.process and self.process.returncode is not None:
                    raise RuntimeError("Owned fixture exited; inspect 8731.log")
                try:
                    r = await client.get("http://127.0.0.1:8731/health")
                    if r.status_code == 200 and r.json().get("fixture") == "burstguard-v2":
                        return r.json()
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(.05)
        raise RuntimeError("Fixture health check timed out")

    async def stop(self, abrupt=False):
        process = self.process
        if process and process.returncode is None:
            process.kill() if abrupt else process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 7)
            except TimeoutError:
                process.kill()
                await process.wait()
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    async def restart(self, mode):
        async def recover():
            async with self.lock:
                if not self.owns_url("http://127.0.0.1:8731"):
                    raise ValueError("소유한 예제 프로세스가 없습니다.")
                before = await self.health()
                start = time.monotonic()
                await self.stop(abrupt=mode == "abrupt")
                after = await self.start()
                return {"before_boot_id": before["boot_id"], "after_boot_id": after["boot_id"],
                        "restart_seconds": round(time.monotonic() - start, 3), "status": "completed"}
        task = asyncio.create_task(recover())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task  # Bring back our fixture even when a user stops the traffic experiment.
            raise
