"""Run only this copy's dashboard and fixture; Ctrl-C stops only our children."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    for port in (8730, 8731, 8732):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Port {port} already in use. Existing processes were left untouched.")
    processes = []
    logs = []
    folder = ROOT / "data" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LANGSMITH_TRACING": "false", "CRASHCUSTOMER_BASE_URL": "http://127.0.0.1:8730", "BURSTGUARD_MANAGE_FIXTURE": "true"}
    try:
        for module, port in (("burstguard.app:app", 8730),):
            log = (folder / f"{port}.log").open("a")
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port),
                "--timeout-graceful-shutdown", "8", "--no-access-log"], cwd=ROOT, env=env, stdout=log, stderr=log))
        print("BurstGuard: http://127.0.0.1:8730/\nFixture: http://127.0.0.1:8731/health\nCtrl-C stops this copy only.", flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(.5)
        raise SystemExit("A child server stopped. Inspect data/logs/8730.log and 8731.log.")
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=12)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        pass
