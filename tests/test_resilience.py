import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from burstguard.analysis import analyze, markdown_report
from burstguard.engine import Runner, aggregate
from burstguard.metrics import MetricsReader
from burstguard.models import ExperimentSpec, Journey, MetricSpec, Step, demo_spec
from burstguard.storage import Records


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1:8730", "http://127.0.0.1:8710", "http://user:pass@localhost:8080", "file:///tmp/app", "http://127.0.0.1:8080?token=x"])
def test_target_boundary(url):
    with pytest.raises(ValidationError):
        ExperimentSpec(**(demo_spec().model_dump() | {"target_url": url}))


@pytest.mark.parametrize("path", ["//example.com/api", "/%2fexample.com", "/../admin", "/%2e%2e/admin", "/\\example.com", "/api\nnext"])
def test_requests_cannot_escape_origin(path):
    with pytest.raises(ValidationError):
        Step(path=path)


def test_runtime_token_not_persisted(tmp_path):
    spec = ExperimentSpec(**(demo_spec().model_dump() | {"token": "super-secret"}))
    store = Records(tmp_path)
    record = store.create(spec)
    assert "super-secret" not in json.dumps(store.get(record["id"]))
    assert "token" not in record["config"]


def test_semantic_failure_and_redirect_are_not_fast_success():
    rows = [{"ok": False, "status": 302, "reason": "unexpected_status", "ms": 1},
            {"ok": False, "status": 200, "reason": "json_assertion", "ms": 2}]
    stats = aggregate(rows, 1)
    assert stats["error_rate"] == 1 and stats["functional_failures"] == 1


class FakeApp:
    def __init__(self):
        self.config = {"adapter": "burstguard-demo-v1", "external_delay_seconds": 0, "external_status": 200,
                       "pool_size": 10, "pool_wait_seconds": .3, "provider_timeout_seconds": 3}
        self.controls = []
        self.calls = []

    def client(self, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **kwargs)

    async def handle(self, request):
        path = request.url.path
        if path.endswith("/__burstguard/control"):
            if request.method == "POST":
                data = json.loads(request.content)
                self.controls.append(data)
                self.config.update(data)
            return httpx.Response(200, json=self.config.copy())
        if path.endswith("/__burstguard/metrics"):
            delayed = self.config["external_delay_seconds"] > 0
            return httpx.Response(200, json={"pool_active": 10 if delayed else 0, "pool_max": 10, "pool_pending": 5 if delayed else 0})
        self.calls.append((path, request.headers.get("authorization")))
        delayed = self.config["external_delay_seconds"] > 0 and "/vulnerable/" in path
        return httpx.Response(503 if delayed else 200, json={"items": [], "access_token": "one-user-token"})


@pytest.mark.asyncio
async def test_full_graph_mixed_traffic_comparison_and_restore(tmp_path):
    fake = FakeApp()
    store = Records(tmp_path)
    runner = Runner(store, client_factory=fake.client)
    spec = ExperimentSpec(**(demo_spec().model_dump() | {"baseline_seconds": .5, "fault_seconds": .5,
        "recovery_seconds": .5, "users": 4, "think_ms": 20, "token": "ephemeral"}))
    record = runner.start(spec)
    await runner.tasks[record["id"]]
    result = store.get(record["id"])
    assert result["status"] == "completed", result.get("error")
    assert len(result["targets"]) == 2
    assert all(t["fault_restore"] == "ok" for t in result["targets"])
    assert result["summary"]["comparison"]["isolation_observed"] is True
    assert fake.config["external_delay_seconds"] == 0
    assert any("login" in p for p, _ in fake.calls) and any("search" in p for p, _ in fake.calls)
    assert all(header == "Bearer ephemeral" for _, header in fake.calls)
    assert "ephemeral" not in json.dumps(result)
    assert "실제 경과 시간" in markdown_report(result)


@pytest.mark.asyncio
async def test_cancellation_restores_injected_fault(tmp_path):
    fake = FakeApp()
    store = Records(tmp_path)
    runner = Runner(store, client_factory=fake.client)
    spec = ExperimentSpec(**(demo_spec().model_dump() | {"comparison_url": None, "baseline_seconds": .5, "fault_seconds": 10, "users": 4}))
    record = runner.start(spec)
    for _ in range(200):
        if fake.config["external_delay_seconds"] > 0:
            break
        await asyncio.sleep(.01)
    assert fake.config["external_delay_seconds"] > 0
    runner.tasks[record["id"]].cancel()
    await runner.tasks[record["id"]]
    result = store.get(record["id"])
    assert result["status"] == "cancelled"
    assert result["targets"][0]["fault_restore"] == "ok"
    assert fake.config["external_delay_seconds"] == 0


@pytest.mark.asyncio
async def test_preflight_auth_failure_stops_before_load(tmp_path):
    calls = []
    async def handle(request):
        calls.append(request.url.path)
        return httpx.Response(401)
    runner = Runner(Records(tmp_path), client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw))
    spec = ExperimentSpec(**(demo_spec().model_dump() | {"fault_mode": "none", "comparison_url": None}))
    record = runner.start(spec)
    await runner.tasks[record["id"]]
    result = runner.records.get(record["id"])
    assert result["status"] == "error"
    assert result["targets"][0]["phases"] == [] and len(calls) == 1
    assert result["summary"] is None


@pytest.mark.asyncio
async def test_real_metrics_mapping_and_missing_instruments():
    async def handle(request):
        name = request.url.path.rsplit("/", 1)[-1]
        if not name.startswith("hikaricp"):
            return httpx.Response(404)
        assert request.url.params["tag"] == "pool:main"
        return httpx.Response(200, json={"measurements": [{"statistic": "VALUE", "value": 7}]})
    async with httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handle)) as client:
        result = await MetricsReader(client, MetricSpec(mode="actuator", path="/actuator/metrics", pool="main")).read()
    assert result["pool_active"] == 7 and result["pool_pending"] == 7
    assert "threads_busy" not in result


@pytest.mark.asyncio
async def test_sessions_and_extract_not_saved(tmp_path):
    async def handle(request):
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "secret-token"})
        assert request.url.params["session"] == "secret-token"
        return httpx.Response(200, json={"items": []})
    runner = Runner(Records(tmp_path))
    async with httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handle)) as client:
        variables = {"vu": 1}
        first = await runner.request_step(client, Step(path="/login", extract={"session": "token"}), variables)
        second = await runner.request_step(client, Step(path="/foods?session={{session}}", expect_json="items"), variables)
    assert first["ok"] and second["ok"]
    assert "secret-token" not in json.dumps([first, second])


def test_missing_pool_metrics_never_claims_pool_exhaustion():
    spec = demo_spec()
    totals = {"requests": 20, "failures": 20, "error_rate": 1, "p95_ms": 4000, "statuses": {"timeout": 20}}
    record = {"targets": [{"label": "test", "phases": [{"name": "fault", "total": totals, "by_journey": {}, "metrics": []}]}]}
    result = analyze(record, spec)["targets"][0]
    assert not result["pool_observed"]
    assert "pool_exhaustion" not in [f["code"] for f in result["findings"]]
    assert any("DB 풀 지표가 없어" in s for s in result["limits"])


@pytest.mark.asyncio
async def test_business_error_in_200_response_is_failure(tmp_path):
    runner = Runner(Records(tmp_path))
    async with httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": False, "data": None}))) as client:
        result = await runner.request_step(client, Step(path="/foods", expect_values={"status": True}), {})
    assert result["ok"] is False and result["reason"] == "json_assertion"


@pytest.mark.asyncio
async def test_header_extraction_auth_is_scoped_to_virtual_user(tmp_path):
    def handle(request):
        if request.url.path == "/login":
            return httpx.Response(200, headers={"Authorization": "Bearer local-session"})
        assert request.headers["Authorization"] == "Bearer local-session"
        return httpx.Response(200)
    runner = Runner(Records(tmp_path))
    async with httpx.AsyncClient(base_url="http://localhost:8080", transport=httpx.MockTransport(handle)) as client:
        variables = {}
        await runner.request_step(client, Step(path="/login", extract_headers={"session": "Authorization"}), variables)
        result = await runner.request_step(client, Step(path="/foods", bearer_variable="session"), variables)
    assert result["ok"] and "local-session" not in json.dumps(result)


@pytest.mark.asyncio
async def test_actual_sqlite_pool_timeout_and_cancel_release(tmp_path):
    import sqlite3
    from fastapi import HTTPException
    from examples.resilience_app import Pool
    pool = Pool(tmp_path / "actual.sqlite3", 1)
    holding = asyncio.Event()

    async def hold():
        async with pool.connection(.1) as connection:
            assert isinstance(connection, sqlite3.Connection)
            connection.execute("BEGIN")
            connection.execute("SELECT 1").fetchone()
            holding.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await holding.wait()
    try:
        with pytest.raises(HTTPException) as failure:
            async with pool.connection(.05):
                pytest.fail("one connection must already be occupied")
        assert failure.value.status_code == 503 and pool.active == 1 and pool.timeouts == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert pool.active == 0 and pool.pending == 0
    async with pool.connection(.1) as connection:
        assert not connection.in_transaction
        assert connection.execute("SELECT 2").fetchone() == (2,)
    pool.close()


@pytest.mark.asyncio
async def test_plan_import_does_not_trust_document_server(tmp_path, monkeypatch):
    import burstguard.app as api_module
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url="http://localhost") as client:
        response = await client.post("/api/resilience/plans/import", files={"file": ("api.yaml", b"openapi: 3.0.0\nservers: [{url: 'https://production.invalid'}]\npaths:\n  /foods:\n    get:\n      summary: list\n")})
        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "openapi" and body["steps"][0]["path"] == "/foods"
        assert "production.invalid" not in json.dumps(body)
        response = await client.post("/api/resilience/plans/import", files={"file": ("bad.yaml", b"- wrong\n- shape\n")})
        assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,expected", [("t0.pool", "completed"), ("t1.pool", "unavailable")])
async def test_ai_cannot_invent_pool_finding_for_healthy_target(monkeypatch, choice, expected):
    import burstguard.analysis as analysis_module
    spec = demo_spec()
    record = {"config": spec.model_dump(), "targets": [
        {"label": "변경 전", "url": spec.target_url, "phases": [{"name": "fault", "metrics": [{"pool_active": 10, "pool_pending": 20}]}]},
        {"label": "변경 후", "url": spec.comparison_url, "phases": [{"name": "fault", "metrics": [{"pool_active": 1, "pool_pending": 0}]}]}],
        "summary": {"targets": [
            {"findings": [{"code": "pool_exhaustion", "evidence": "full"}], "protected_affected": True, "limits": []},
            {"findings": [{"code": "request_failures", "evidence": "login timeout"}], "protected_affected": False, "limits": []}]}}
    original = httpx.AsyncClient

    def handle(request):
        body = json.loads(request.content)
        choices = body["format"]["properties"]["priorities"]["items"]["enum"]
        assert "t0.pool" in choices and "t1.pool" not in choices
        return httpx.Response(200, json={"message": {"content": json.dumps({"priorities": [choice]})}, "model": "test"})

    monkeypatch.setattr(analysis_module.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    result = await analysis_module.ai_analysis(record)
    assert result["status"] == expected
    if expected == "completed":
        assert result["proposals"][0]["patch"]["pool_size"] == 20
        assert result["proposals"][0]["patch"]["target_url"] == spec.target_url
