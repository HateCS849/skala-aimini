"""Frozen same-input paired comparison of full vs compact NEW main planning prompts."""
import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path

from burstguard.agents import EvidenceContext, OllamaModel, candidates, planning_payload, plan_schema, checked_plan
from burstguard.models import demo_spec


async def main(repeats, output):
    fixtures = []
    goals = ["login pool isolation 외부 로그인 지연과 독립 조회 장애를 분리해줘",
             "retry external 429 외부 재시도 증폭과 동시 접속 영향을 분리해줘",
             "stock integrity idempotency 같은 키 중복 주문과 재고 정합성을 확인해줘"]
    async with EvidenceContext() as context:
        for goal in goals:
            ctx = await context.context(goal, None)
            fixtures.append({"goal": goal, "context": ctx, "options": candidates(demo_spec()), "previous": None})
    frozen = json.dumps(fixtures, ensure_ascii=False, sort_keys=True)
    fixture_path = output.with_name("resilience-agent-context-fixtures-20261022.json")
    fixture_path.write_text(frozen + "\n", encoding="utf-8")
    model, rows = OllamaModel(), []
    for repeat in range(repeats):
        for index, fixture in enumerate(fixtures):
            for mode in (("full", "compact") if (repeat + index) % 2 == 0 else ("compact", "full")):
                payload = planning_payload(fixture["goal"], fixture["context"], fixture["options"], fixture["previous"], mode)
                started = time.perf_counter()
                try:
                    answer, usage = await model.choose("planner", payload, plan_schema(fixture["options"], fixture["context"]["references"]))
                    checked_plan(answer, fixture["options"], fixture["context"]["references"])
                    row = {"valid": True, "plan": answer, **usage}
                except Exception as error:
                    row = {"valid": False, "error": type(error).__name__}
                row.update(repeat=repeat + 1, fixture=index, mode=mode, ms=round((time.perf_counter() - started) * 1000, 2))
                rows.append(row)
                output.write_text(json.dumps({"complete": False, "results": rows}, ensure_ascii=False, indent=2))
                print(json.dumps({"repeat": repeat + 1, "fixture": index, "mode": mode, "valid": row["valid"], "input_tokens": row.get("input_tokens"), "ms": row["ms"]}), flush=True)
    summary = {}
    for mode in ("full", "compact"):
        runs = [r for r in rows if r["mode"] == mode]
        valid = [r for r in runs if r["valid"]]
        summary[mode] = {"calls": len(runs), "valid_plans": len(valid), "mean_ms": round(statistics.mean(r["ms"] for r in runs), 2),
            "mean_input_tokens": round(statistics.mean(r.get("input_tokens", 0) for r in valid), 2) if valid else None,
            "mean_output_tokens": round(statistics.mean(r.get("output_tokens", 0) for r in valid), 2) if valid else None}
    pairs = [(next(r for r in rows if r["repeat"] == n+1 and r["fixture"] == f and r["mode"] == "full"),
              next(r for r in rows if r["repeat"] == n+1 and r["fixture"] == f and r["mode"] == "compact")) for n in range(repeats) for f in range(3)]
    summary["same_scenario_pairs"] = sum(a.get("plan", {}).get("scenario_id") == b.get("plan", {}).get("scenario_id") for a, b in pairs if a["valid"] and b["valid"])
    if summary["full"]["mean_input_tokens"]:
        summary["input_token_reduction_percent"] = round(100 * (1 - summary["compact"]["mean_input_tokens"] / summary["full"]["mean_input_tokens"]), 2)
    result = {"complete": True, "fixtures": 3, "repeats": repeats, "fixture_file": fixture_path.name, "fixture_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "scope": "New main planner, identical frozen goals/candidate IDs/evidence/model/schema, alternating paired order. Compact removes repeated full runbook and truncates reference excerpts. Valid ID selection is not diagnostic accuracy or financial cost.",
        "summary": summary, "results": rows}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("docs/benchmark-resilience-agents-20261022.json"))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1..10")
    asyncio.run(main(args.repeats, args.output))
