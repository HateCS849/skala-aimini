"""Run a bounded real HTTP comparison and preserve evidence in this copy's data folder."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--ai", action="store_true")
    args = parser.parse_args()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8730", trust_env=False, timeout=10) as client:
        response = await client.get("/api/resilience/template")
        response.raise_for_status()
        spec = response.json()
        spec.update(users=args.users, analysis="ollama" if args.ai else "rules")
        response = await client.post("/api/resilience/experiments", json=spec)
        response.raise_for_status()
        record = response.json()
        print("Started", record["id"], flush=True)
        for _ in range(300):
            response = await client.get("/api/resilience/experiments/" + record["id"])
            response.raise_for_status()
            record = response.json()
            if record["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(.5)
        else:
            await client.post("/api/resilience/experiments/" + record["id"] + "/cancel")
            raise SystemExit("Timed out; cancellation requested")
        output = Path(__file__).resolve().parent.parent / "data" / ("verification-" + record["id"] + ".json")
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        result = {"id": record["id"], "status": record["status"], "elapsed_seconds": record.get("elapsed_seconds"),
                  "summary": record.get("summary"), "ai_status": record.get("ai", {}).get("status")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if record["status"] != "completed" or not record.get("summary", {}).get("comparison", {}).get("isolation_observed"):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
