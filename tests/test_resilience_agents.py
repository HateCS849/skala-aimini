import asyncio
import json

import httpx
import pytest

from burstguard.agents import Supervisor, EvidenceContext, candidates, checked_plan, planning_payload
from burstguard.engine import Runner
from burstguard.models import ExperimentSpec, demo_spec
from burstguard.storage import Records
from burstguard.validation import evidence_packet, validate_evidence
from crashcustomer.store import Store
from test_resilience import FakeApp


class Model:
    def __init__(self, bad=False):
        self.calls, self.bad = [], bad

    async def choose(self, role, payload, schema):
        self.calls.append((role, payload))
        if self.bad:
            return {"scenario_id": "http://evil.test", "reference_ids": ["fabricated"]}, {}
        if role == "planner":
            ids = [c["id"] for c in payload["candidates"]]
            answer = {"scenario_id": "requested" if "requested" in ids else ids[0],
                      "reference_ids": [r["id"] for r in payload["references"][:1]]}
        else:
            answer = {"priority_ids": [i["id"] for i in payload["checks"]["issues"][:3]]}
        return answer, {"input_tokens": 100, "output_tokens": 12, "model": "test"}


def spec(**values):
    return ExperimentSpec.model_validate(demo_spec().model_dump() | {
        "baseline_seconds": .5, "fault_seconds": .5, "recovery_seconds": .5, "users": 4, "think_ms": 20,
        "agent": {"enabled": True, "max_experiments": 2, "goal": "login pool isolation retry"}, **values})


@pytest.mark.asyncio
async def test_main_supervisor_real_mcp_two_roles_bounded_replan_and_trace(tmp_path):
    data = tmp_path / "project-data"
    store = Store(data)
    p = store.create_project({"name": "test", "browser_url": "http://localhost:8080"})
    store.add_attachment(p["id"], "incident.md", b"login pool isolation: waiting for Apple holds DB connection", {"category": "reference", "format": "md"})
    model, fake = Model(), FakeApp()
    runner = Runner(Records(tmp_path / "runs"), client_factory=fake.client,
        supervisor_factory=lambda: Supervisor(model, lambda: EvidenceContext(data)))
    config = spec(agent={"enabled": True, "project_id": p["id"], "goal": "login pool isolation", "max_experiments": 2}, token="ephemeral")
    r = runner.start(config)
    await runner.tasks[r["id"]]
    result = runner.records.get(r["id"])
    assert result["status"] == "completed", result.get("error")
    a = result["agent"]
    assert len(a["runs"]) == 2 and a["validation"]["requested_tested"]
    assert any(r["source"] == "incident.md" for r in a["references"])
    assert "tools/call:evaluate_evidence" in a["mcp_calls"]
    assert [c[0] for c in model.calls] == ["planner", "validator", "planner", "validator"]
    assert a["usage"]["llm_calls"] == 4 and a["usage"]["input_tokens"] == 400
    assert len([t for t in a["trace"]["runs"] if t["run_type"] == "llm"]) == 4
    assert all(t["end_time"] for t in a["trace"]["runs"])
    assert a["trace"]["cloud_verified"] is False
    assert result["summary"]["comparison"]["isolation_observed"] is True
    assert all(t["fault_restore"] == "ok" for run in a["runs"] for t in run["targets"])
    assert "ephemeral" not in json.dumps(result)
    assert all(h == "Bearer ephemeral" for _, h in fake.calls)
    assert len(runner.records.list()) == 1  # child snapshots never leak as independent history
    from burstguard.analysis import markdown_report
    assert "이 실행의 클라우드 수신은 검증하지 않았습니다" in markdown_report(result)
    a["trace"].update(cloud_verified=True, cloud={"status": "verified", "readback": {"received": 13, "expected": 13},
        "project": "test", "cloud_root_id": "test-root", "checked_at": "test-time"})
    assert "LangSmith 클라우드 수신 확인: 13/13개" in markdown_report(result)


@pytest.mark.asyncio
async def test_untrusted_model_cannot_change_target_or_raise_load(tmp_path):
    fake = FakeApp()
    runner = Runner(Records(tmp_path / "runs"), client_factory=fake.client,
        supervisor_factory=lambda: Supervisor(Model(bad=True), lambda: EvidenceContext(tmp_path / "mcp")))
    r = runner.start(spec(agent={"enabled": True, "max_experiments": 1, "goal": "login pool"}))
    await runner.tasks[r["id"]]
    a = r["agent"]
    assert r["status"] == "completed"
    assert a["plans"][0]["scenario_id"] == "requested" and a["plans"][0]["backend"] == "fallback"
    assert a["validation"]["verdict"] == "partial" and a["limitations"]


@pytest.mark.asyncio
async def test_failed_preflight_stops_replanning_and_never_claims_complete(tmp_path):
    calls = []
    async def handle(request):
        calls.append(request.url.path)
        return httpx.Response(401)
    runner = Runner(Records(tmp_path / "runs"), client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw),
        supervisor_factory=lambda: Supervisor(Model(), lambda: EvidenceContext(tmp_path / "mcp")))
    r = runner.start(spec(fault_mode="none", comparison_url=None))
    await runner.tasks[r["id"]]
    assert r["status"] == "error" and len(r["agent"]["runs"]) == 1
    assert r["agent"]["validation"]["verdict"] == "partial" and len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_supervisor_restores_actual_worker_config(tmp_path):
    fake = FakeApp()
    runner = Runner(Records(tmp_path / "runs"), client_factory=fake.client,
        supervisor_factory=lambda: Supervisor(Model(), lambda: EvidenceContext(tmp_path / "mcp")))
    r = runner.start(spec(fault_seconds=10))
    for _ in range(600):
        if fake.config["external_delay_seconds"]:
            break
        await asyncio.sleep(.01)
    assert fake.config["external_delay_seconds"] > 0
    runner.tasks[r["id"]].cancel()
    await runner.tasks[r["id"]]
    assert r["status"] == "cancelled"
    assert fake.config["external_delay_seconds"] == 0
    assert r["agent"]["trace"]["finished"]


def test_validator_missing_state_or_short_samples_never_pass():
    packet = {"execution_status": "completed", "mode": "load", "metrics_mode": "json", "integrity_enabled": True,
        "targets": [{"label": "test", "phases": [], "checks": ["unverified"], "restore": "failed"}]}
    v = validate_evidence(packet)
    assert v["verdict"] == "partial"
    assert {i["id"] for i in v["issues"]} >= {"t0.restore", "t0.samples.fault", "t0.metrics", "t0.integrity"}


def test_cost_compaction_preserves_candidates_and_all_numeric_evidence():
    config = spec()
    opts = candidates(config)
    packet = {"pool": 10, "requests": 70}
    context = {"prompt": "policy", "runbook": "duplicate " * 1000,
        "references": [{"id": "ref0", "source": "test.md", "text": "evidence " * 1000}]}
    full = planning_payload("test", context, opts, packet, "full")
    compact = planning_payload("test", context, opts, packet, "compact")
    assert compact["candidates"] == full["candidates"] and compact["previous"] == full["previous"]
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 2
    with pytest.raises(ValueError):
        checked_plan({"scenario_id": "requested", "reference_ids": ["fake"]}, opts, context["references"])


@pytest.mark.asyncio
async def test_repeated_integrity_is_bounded_even_when_scenario_ids_repeat(tmp_path, monkeypatch):
    """Budget counts executions, not unique IDs: requested/repeat/repeat must terminate."""
    from burstguard.presets import all_presets
    base = next(p["spec"] for p in all_presets() if p["id"] == "integrity")
    class WorkerModel(Model):
        async def choose(self, role, payload, schema):
            if role == "planner":
                return {"scenario_id": payload["candidates"][0]["id"], "reference_ids": []}, {}
            return {"priority_ids": []}, {}
    async def execute_plain(self, child, config):
        child.update(status="completed", targets=[{"label": "test", "phases": [], "fault_restore": "ok",
            "integrity": [{"checks": [{"status": "failed"}]}]}], summary={"targets": [{"label": "test", "findings": [{"code": "integrity_stock", "title": "stock", "evidence": "-1"}], "limits": []}]})
        self.records.save(child)
    monkeypatch.setattr(Runner, "execute_plain", execute_plain)
    runner = Runner(Records(tmp_path / "runs"), supervisor_factory=lambda: Supervisor(WorkerModel(), lambda: EvidenceContext(tmp_path / "mcp")))
    config = ExperimentSpec.model_validate(base | {"comparison_url": None, "agent": {"enabled": True, "max_experiments": 3, "goal": "stock integrity"}})
    r = runner.start(config)
    await runner.tasks[r["id"]]
    assert r["status"] == "completed" and len(r["agent"]["runs"]) == 3
    assert [p["scenario_id"] for p in r["agent"]["plans"]] == ["requested", "repeat", "repeat"]


@pytest.mark.asyncio
async def test_model_failure_is_labeled_and_offline_trace_never_posts(tmp_path, monkeypatch):
    from langsmith.run_trees import RunTree
    def forbidden(*args, **kwargs):
        pytest.fail("Offline supervisor must not post SDK traces")
    monkeypatch.setattr(RunTree, "post", forbidden)
    class Unavailable:
        async def choose(self, *args):
            raise httpx.ConnectError("offline")
    fake = FakeApp()
    runner = Runner(Records(tmp_path / "runs"), client_factory=fake.client,
        supervisor_factory=lambda: Supervisor(Unavailable(), lambda: EvidenceContext(tmp_path / "mcp")))
    r = runner.start(spec(agent={"enabled": True, "max_experiments": 1, "goal": "login pool"}))
    await runner.tasks[r["id"]]
    a = r["agent"]
    assert r["status"] == "completed" and a["validation"]["verdict"] == "partial"
    assert a["usage"]["llm_attempts"] == 2 and a["usage"]["llm_calls"] == 0
    assert a["plans"][0]["backend"] == "fallback" and a["trace"]["delivery"] == "local_only"


def test_unobserved_protected_function_is_not_evidence_complete():
    p = {"execution_status": "completed", "mode": "load", "metrics_mode": "none", "expects_protected": True,
        "targets": [{"label": "test", "restore": "ok", "protected_observed": False,
            "phases": [{"name": n, "requests": 20} for n in ["baseline", "fault", "recovery"]]}]}
    assert validate_evidence(p)["issues"] == [{"id": "t0.protected", "message": "보호 기능의 기준·장애 표본 또는 정상 기준이 부족합니다."}]


def test_contrast_reports_observed_change_only_with_sufficient_control():
    from burstguard.validation import contrast_experiments
    def run(scenario, affected, verdict):
        return {"scenario_id": scenario, "validation": {"verdict": verdict},
            "summary": {"targets": [{"label": "test", "protected_affected": affected, "findings": []}]}}
    original, control = run("requested", True, "complete"), run("no_fault", False, "partial")
    assert contrast_experiments([original, control])[0]["status"] == "unverified"
    control["validation"]["verdict"] = "complete"
    assert contrast_experiments([original, control])[0]["status"] == "observed_change"
