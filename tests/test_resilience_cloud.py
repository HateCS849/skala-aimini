import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from burstguard.cloud_trace import metadata_payload, upload_and_verify, verify_readback


def record():
    root, child = str(uuid4()), str(uuid4())
    stamp = datetime.now(timezone.utc).isoformat()
    return {"id": "a" * 32, "status": "completed", "config": {"target_url": "http://127.0.0.1:8731/vulnerable",
        "comparison_url": "http://127.0.0.1:8731/isolated", "fault_mode": "demo", "token": "NEVER-UPLOAD"},
        "agent": {"goal": "NEVER-UPLOAD", "references": [{"text": "NEVER-UPLOAD"}],
            "usage": {"llm_calls": 1, "input_tokens": 200, "output_tokens": 20},
            "mcp_calls": ["tools/call:search_runbooks", "NEVER-UPLOAD"], "trace": {"root_id": root, "runs": [
                {"id": root, "name": "burstguard.supervisor", "run_type": "chain", "start_time": stamp, "end_time": stamp,
                 "parent_run_id": None, "inputs": {"private": "NEVER-UPLOAD"}, "outputs": {"status": "completed", "experiments": 1, "validation": "complete", "secret": "NEVER-UPLOAD"}},
                {"id": child, "name": "burstguard.planner", "run_type": "llm", "start_time": stamp, "end_time": stamp,
                 "parent_run_id": root, "inputs": {"attempt": 1, "prompt": "NEVER-UPLOAD"}, "outputs": {"status": "completed", "llm_calls": 1}}]}}}


def test_projection_is_allowlisted_and_never_exports_content():
    r = record()
    p = metadata_payload(r)
    assert "NEVER-UPLOAD" not in json.dumps(p, default=str)
    assert p["root_id"] != r["agent"]["trace"]["root_id"]
    assert p["runs"][1]["parent_run_id"] == p["root_id"]
    assert p["runs"][0]["outputs"]["recorded_mcp_calls"] == ["tools/call:search_runbooks"]


@pytest.mark.parametrize("kind", ["live", "unfinished", "unknown_role", "wrong_parent"])
def test_rejects_non_synthetic_or_inconsistent_records(kind):
    r = record()
    if kind == "live": r["config"]["target_url"] = "http://localhost:8080"
    if kind == "unfinished": r["status"] = "running"
    if kind == "unknown_role": r["agent"]["trace"]["runs"][1]["name"] = "untrusted-secret"
    if kind == "wrong_parent": r["agent"]["trace"]["runs"][1]["parent_run_id"] = str(uuid4())
    with pytest.raises(ValueError): metadata_payload(r)


def received(row):
    return SimpleNamespace(**{k: row[k] for k in ("id", "name", "run_type", "trace_id", "parent_run_id", "inputs", "outputs", "start_time", "end_time")}, error=None, url="https://smith.langchain.com/example")


def test_readback_needs_every_span_and_exact_nonce_outputs():
    p = metadata_payload(record())
    rows = [received(r) for r in p["runs"]]
    assert verify_readback(p, rows)["verified"]
    assert not verify_readback(p, rows[:1])["verified"]
    rows[1].outputs = {"status": "completed"}
    assert verify_readback(p, rows)["invalid_ids"] == [p["runs"][1]["id"]]


def test_exporter_receipt_requires_readback():
    p = metadata_payload(record())
    class Client:
        def __init__(self, **kwargs): self.rows = {}; self.closed = False
        def create_run(self, **kwargs):
            assert kwargs.pop("project_name") == "burstguard-cloud-verification"
            self.rows[kwargs["id"]] = kwargs
        def read_run(self, run_id): return received(self.rows[run_id])
        def close(self): self.closed = True
    result = upload_and_verify(p, "synthetic-key", client_factory=Client, waits=(0,))
    assert result["status"] == "verified" and result["readback"]["received"] == 2
    assert result["completed_llm_spans"] == 1 and result["synthetic_only"]
    assert "synthetic-key" not in json.dumps(result)
