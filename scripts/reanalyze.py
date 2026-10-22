"""Reanalyze already collected synthetic or local evidence without generating more load."""
import argparse
import asyncio
import json

from burstguard.analysis import ai_analysis
from burstguard.storage import Records, now


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("record_id")
    args = parser.parse_args()
    store = Records()
    record = store.get(args.record_id)
    if not record or not record.get("summary") or record["status"] in {"running", "queued"}:
        raise SystemExit("Completed measurement evidence required")
    record["ai"] = await ai_analysis(record)
    record["ai_checked_at"] = now()
    store.save(record)
    print(json.dumps(record["ai"], ensure_ascii=False, indent=2))
    if record["ai"]["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
