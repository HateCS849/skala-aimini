import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from burstguard.models import ExperimentSpec, FaultSpec, demo_spec
from burstguard.engine import Runner
from burstguard.storage import Records
from burstguard.diagnostics import counter_delta, trend
from burstguard.integrity import run_integrity
from burstguard.presets import all_presets
from examples.process_metrics import process_metrics


def test_all_presets_are_executable_valid_models():
    presets = all_presets()
    assert len(presets) == 10
    for preset in presets:
        spec = ExperimentSpec.model_validate(preset["spec"])
        assert spec.target_url.startswith("http://127.0.0.1:8731/")


@pytest.mark.parametrize("patch", [{"fault_mode": "none", "faults": {"cache_unavailable": True}},
    {"fault_mode": "none", "restart": {"enabled": True}}, {"restart": {"enabled": True, "at_seconds": 10}, "fault_seconds": 5},
    {"mode": "integrity", "integrity": {"enabled": False}}])
def test_invalid_unsupported_experiments_rejected(patch):
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(demo_spec().model_dump() | patch)


def test_fault_reset_only_touches_requested_extensions():
    assert FaultSpec().payload(True) == {}
    fault = FaultSpec(db_delay_ms=300, cache_unavailable=True)
    assert fault.payload(True) == {"db_delay_ms": 300, "cache_unavailable": True}
    assert fault.payload(False) == {"db_delay_ms": 0, "cache_unavailable": False}


def test_counter_reset_never_generates_retry_ratio():
    assert counter_delta([{"n": 10}, {"n": 0}, {"n": 30}], "n") is None
    assert counter_delta([{"n": 5}, {"n": 7}, {"n": 12}], "n") == 7


def test_short_soak_is_inconclusive_even_if_http_is_fast():
    from burstguard.analysis import analyze
    spec = ExperimentSpec.model_validate(next(p for p in all_presets() if p["id"] == "soak")["spec"])
    record = {"targets": [{"label": "test", "phases": [{"name": "fault", "total": {"requests": 100, "error_rate": 0, "p95_ms": 1}, "by_journey": {}, "metrics": []}]}]}
    result = analyze(record, spec)["targets"][0]
    assert result["verdict"] == "insufficient"


@pytest.mark.asyncio
async def test_cancel_during_owned_restart_still_brings_fixture_back():
    from burstguard.fixture import Fixture
    fixture = Fixture()
    reached_stop, allow_start = asyncio.Event(), asyncio.Event()
    running = True

    async def health():
        return {"boot_id": "before" if running else "after"}

    async def stop(abrupt=False):
        nonlocal running
        running = False
        reached_stop.set()

    async def start():
        nonlocal running
        await allow_start.wait()
        running = True
        return {"boot_id": "after"}

    fixture.owns_url = lambda url: running
    fixture.health, fixture.stop, fixture.start = health, stop, start
    task = asyncio.create_task(fixture.restart("abrupt"))
    await reached_stop.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_start.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert running


def test_resource_growth_requires_duration_samples_and_absolute_change():
    points = [{"second": i, "rss": 1000 + i * 100} for i in range(41)]
    assert trend(points, "rss", 30, 500)["status"] == "growth"
    assert trend(points[:5], "rss", 30, 500)["status"] == "unverified"
    assert trend(points, "rss", 30, 10000)["status"] == "no_growth_observed"
    flat = [{"second": i, "rss": 2000} for i in range(41)]
    assert trend(flat, "rss", 30, 1)["status"] == "no_growth_observed"


def test_heap_growth_remains_visible_when_os_compresses_resident_pages():
    from burstguard.diagnostics import extended
    spec = ExperimentSpec.model_validate(next(p for p in all_presets() if p["id"] == "soak")["spec"])
    mib = 1024 * 1024
    points = [{"second": i, "process_rss_bytes": (100-i)*mib, "heap_used_bytes": (50+2*i)*mib, "open_fds": 20, "process_threads": 1} for i in range(41)]
    target = {"phases": [{"name": "fault", "metrics": points}]}
    findings, _ = extended(target, spec)
    codes = {f["code"] for f in findings}
    assert "growth_heap_used_bytes" in codes and "growth_process_rss_bytes" not in codes


def test_current_process_memory_is_real_positive_metric():
    metrics = process_metrics()
    assert metrics["process_rss_bytes"] > 0
    assert metrics["open_fds"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_keys,reject_all", [(True, False), (False, True)])
async def test_idempotency_cannot_pass_without_observable_durable_effect(tmp_path, missing_keys, reject_all):
    preset = next(p for p in all_presets() if p["id"] == "integrity")["spec"]
    spec = ExperimentSpec.model_validate(preset | {"comparison_url": None, "integrity": preset["integrity"] | {"rounds": 1, "cases": ["idempotency"]}})
    writes = 0

    async def handle(request):
        nonlocal writes
        if request.url.path.endswith("/orders/reset"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/orders/state"):
            state = {"stock": 5 - writes, "orders": writes}
            if not missing_keys:
                state["unique_keys"] = writes
            return httpx.Response(200, json=state)
        if reject_all:
            return httpx.Response(409)
        writes = 1
        return httpx.Response(200, json={})

    runner = Runner(Records(tmp_path), client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw))
    record = runner.records.create(spec)
    target = {"url": spec.target_url, "label": "test"}
    record["targets"].append(target)
    await run_integrity(runner, record, target, spec)
    check = next(c for c in target["integrity"][0]["checks"] if c["code"] == "idempotency")
    assert check["status"] == ("unverified" if missing_keys else "failed")


@pytest.mark.asyncio
async def test_restart_rejects_unmanaged_fixture_before_any_load(tmp_path):
    runner = Runner(Records(tmp_path))
    spec = ExperimentSpec.model_validate(next(p for p in all_presets() if p["id"] == "restart")["spec"])
    record = runner.start(spec)
    await runner.tasks[record["id"]]
    result = runner.records.get(record["id"])
    assert result["status"] == "error" and result["targets"] == []


@pytest.mark.asyncio
async def test_baseline_clears_supported_fault_then_restores_original_setting(tmp_path):
    preset = next(p for p in all_presets() if p["id"] == "cache")["spec"]
    preset.update(comparison_url=None, baseline_seconds=.5, fault_seconds=.5, recovery_seconds=.5, users=2)
    # Explicitly request normal cache; the existing fixture is initially faulted.
    preset["faults"]["cache_unavailable"] = False
    spec = ExperimentSpec.model_validate(preset)
    config = {"adapter": "burstguard-demo-v1", "cache_unavailable": True}
    posted = []

    async def handle(request):
        if request.url.path.endswith("/control"):
            if request.method == "POST":
                patch = json.loads(request.content)
                posted.append(patch)
                config.update(patch)
            return httpx.Response(200, json=config.copy())
        if request.url.path.endswith("/metrics"):
            return httpx.Response(200, json={"pool_active": 0, "pool_max": 10})
        return httpx.Response(503 if config["cache_unavailable"] else 200, json={"items": []})

    runner = Runner(Records(tmp_path), client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw))
    record = runner.start(spec)
    await runner.tasks[record["id"]]
    assert runner.records.get(record["id"])["status"] == "completed"
    assert posted[0]["cache_unavailable"] is False and posted[-1]["cache_unavailable"] is True


@pytest.mark.asyncio
async def test_redis_gate_closes_only_owned_variant_and_passes_real_reply():
    from examples.redis_wire import encode, encode_command, read
    # Protocol parser itself is tested without opening local sockets in unit tests.
    reader = asyncio.StreamReader()
    reader.feed_data(encode(["GET", "burstguard:isolated:foods"]))
    reader.feed_eof()
    assert await read(reader) == [b"GET", b"burstguard:isolated:foods"]
    reader = asyncio.StreamReader()
    reader.feed_data(encode_command(["SET", "burstguard:isolated:foods", "value", "EX", 5]))
    reader.feed_eof()
    assert await read(reader) == [b"SET", b"burstguard:isolated:foods", b"value", b"EX", b"5"]
    reader = asyncio.StreamReader()
    reader.feed_eof()
    with pytest.raises(ConnectionError):
        await read(reader)
