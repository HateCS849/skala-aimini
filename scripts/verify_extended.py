"""Real localhost HTTP verification, including a real Redis and owned-process restart."""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", default=["integrity", "retry", "malformed", "disconnect", "cache", "db-delay", "db-lock", "restart", "soak"])
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "extended-verification.json")
    args = parser.parse_args()
    evidence = {"date": datetime.now(timezone.utc).isoformat(), "scope": "Real local synthetic HTTP/SQLite/Redis fixtures; not Hangari or production capacity.", "results": []}
    destination = args.output
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8730", timeout=15, trust_env=False) as client:
        response = await client.get("/api/resilience/presets")
        response.raise_for_status()
        presets = {p["id"]: p["spec"] for p in response.json()}
        for key in args.only:
            spec = presets[key]
            spec.update(baseline_seconds=1, fault_seconds=2, recovery_seconds=1, analysis="rules")
            if key == "soak":
                spec.update(fault_seconds=45, think_ms=500)
            elif key == "restart":
                spec.update(fault_seconds=5)
                spec["restart"]["at_seconds"] = 1
            elif key == "db-delay":
                spec["faults"]["db_delay_ms"] = 300
                spec["slo_ms"] = 100
            elif key == "db-lock":
                spec["faults"]["db_lock_seconds"] = 1.5
                spec["users"] = 2
            elif key == "disconnect":
                spec.update(users=2, think_ms=300)
            response = await client.post("/api/resilience/experiments", json=spec)
            response.raise_for_status()
            record_id = response.json()["id"]
            print(f"{key}: started {record_id}", flush=True)
            for _ in range(360):
                response = await client.get("/api/resilience/experiments/" + record_id)
                response.raise_for_status()
                record = response.json()
                if record["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(.5)
            else:
                await client.post("/api/resilience/experiments/" + record_id + "/cancel")
                raise RuntimeError("Verification timeout")
            codes = [[f["code"] for f in result["findings"]] for result in (record.get("summary") or {}).get("targets", [])]
            valid = record["status"] == "completed" and len(codes) == 2
            if valid:
                if key == "integrity":
                    valid = "integrity_no_oversell" in codes[0] and "integrity_idempotency" in codes[0] and not codes[1]
                elif key in {"retry", "malformed", "disconnect"}:
                    valid = "retry_amplification" in codes[0] and "retry_amplification" not in codes[1]
                elif key == "cache":
                    valid = "cache_dependency_errors" in codes[0] and record["summary"]["comparison"]["isolation_observed"]
                elif key == "db-delay":
                    valid = "latency_slo" in codes[0]
                elif key == "db-lock":
                    valid = all("db_lock_contention" in group for group in codes)
                elif key == "restart":
                    valid = all(t.get("restarts") and all(r.get("before_boot_id") != r.get("after_boot_id") and r.get("data_status") == "passed" for r in t["restarts"]) for t in record["targets"])
                elif key == "soak":
                    growth = {"growth_process_rss_bytes", "growth_heap_used_bytes"}
                    valid = bool(growth.intersection(codes[0])) and not growth.intersection(codes[1])
            evidence["results"].append({"preset": key, "passed": valid, "record": record})
            destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            print(f"{key}: {'PASS' if valid else 'FAIL'} · {record['status']} · {record.get('error')} · {codes}", flush=True)
    if not all(r["passed"] for r in evidence["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
