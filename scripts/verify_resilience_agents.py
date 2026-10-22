"""Live main API + real MCP + local Ollama, using only the owned synthetic fixture."""
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8730", timeout=120, trust_env=False) as client:
        spec = (await client.get("/api/resilience/template")).json()
        projects = (await client.get("/api/projects")).json()
        project = next((p for p in projects if p["name"] == "BurstGuard Agent Evaluation · Synthetic"), None)
        if project is None:
            response = await client.post("/api/projects", json={"name": "BurstGuard Agent Evaluation · Synthetic",
                "browser_url": "http://127.0.0.1:8731/vulnerable", "description": "AI 감독 검증용 합성 Runbook. 실제 Hangari 데이터 없음."})
            response.raise_for_status()
            project = response.json()
            upload = await client.post(f"/api/projects/{project['id']}/attachments", files={"file": (
                "synthetic-pool-runbook.md", "# login pool isolation\n외부 로그인 지연에서 DB 풀과 음식 목록·검색의 오류율을 비교한다. 낮은 부하 또는 장애 해제 대조로 분리한다.\n", "text/markdown")})
            upload.raise_for_status()
        spec.update(name="AI 감독 · 합성 풀 장애 전파 검증", users=10, baseline_seconds=1, fault_seconds=2,
            recovery_seconds=1, think_ms=20, pool_size=2, external_delay_seconds=1,
            provider_timeout_seconds=.2, pool_wait_seconds=.1, timeout_seconds=2,
            agent={"enabled": True, "goal": "login pool isolation: 외부 로그인 지연에서 음식 목록 검색 장애 전파를 확인하고 대조 실험해줘.",
                   "project_id": project["id"], "backend": "ollama", "max_experiments": 2})
        response = await client.post("/api/resilience/experiments", json=spec)
        response.raise_for_status()
        run_id = response.json()["id"]
        print(json.dumps({"started": run_id}), flush=True)
        updates, roles, final = 0, set(), None
        async with client.stream("GET", f"/api/resilience/experiments/{run_id}/events", timeout=300) as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if "status" in data:
                        updates += 1
                        final = data
                        for r in data.get("agent", {}).get("roles", []):
                            roles.add(r["name"])
        assert final["status"] == "completed", final.get("error")
        agent = final["agent"]
        assert agent["trace"]["finished"] and len(agent["runs"]) == 2
        assert agent["validation"]["requested_tested"]
        assert all(p["backend"] == "ollama" for p in agent["plans"]), agent["limitations"]
        assert all(r["validation"]["backend"] == "ollama" for r in agent["runs"]), agent["limitations"]
        assert any(r["source"] == "synthetic-pool-runbook.md" for r in agent["references"])
        assert final["summary"]["comparison"]["isolation_observed"] is True
        assert {"research", "planner", "executor", "validator", "replan_check", "write"} <= roles
        trace = (await client.get(f"/api/resilience/experiments/{run_id}/trace")).json()
        assert trace["cloud_verified"] is False and len(trace["runs"]) >= 10
        report = await client.get(f"/api/resilience/experiments/{run_id}/report")
        assert "AI 감독" in report.text and "독립 검증" in report.text
        (Path("docs") / "agent-live-report-20261022.md").write_text(report.text, encoding="utf-8")
        result = {"run_id": run_id, "synthetic_only": True, "status": final["status"],
            "sse_updates": updates, "roles": sorted(roles), "usage": agent["usage"], "plans": agent["plans"],
            "mcp_calls": agent["mcp_calls"], "references": [{"source": r["source"], "method": r["method"]} for r in agent["references"]],
            "validation": agent["validation"], "comparison": final["summary"]["comparison"],
            "contrasts": agent.get("contrasts", []),
            "experiments": [{"scenario_id": r["scenario_id"], "validation": r["validation"],
                "targets": [{"label": t["label"], "fault_restore": t["fault_restore"],
                    "phases": [{"name": p["name"], "total": p.get("total"), "by_journey": p.get("by_journey")} for p in t["phases"]]} for t in r["targets"]]} for r in agent["runs"]],
            "trace": {"provider": trace["provider"], "delivery": trace["delivery"], "cloud_verified": False, "spans": len(trace["runs"])},
            "all_restored": all(t["fault_restore"] == "ok" for r in agent["runs"] for t in r["targets"])}
        Path("docs/agent-live-trace-20261022.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        Path("docs/agent-live-evidence-20261022.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
