"""Explicit, metadata-only export of a completed main run and strict cloud readback.

No environment tracing switch is enabled. Arbitrary local trace dictionaries are never
forwarded: names, inputs, outputs and metadata are rebuilt from allowlisted fields.
"""
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from langsmith import Client
from langsmith.utils import LangSmithNotFoundError

ROLES = {"burstguard.supervisor": "chain", "burstguard.research": "tool", "burstguard.planner": "llm",
    "burstguard.gate": "chain", "burstguard.executor": "tool", "burstguard.validator": "llm",
    "burstguard.replan_check": "chain", "burstguard.write": "chain"}
MCP_CALLS = {"prompts/get:resilience_plan", "resources/read:burstguard-default", "tools/call:search_runbooks", "tools/call:evaluate_evidence"}


def count(value):
    return value if type(value) is int and 0 <= value <= 10000000 else 0


def metadata_payload(record):
    if record.get("status") != "completed":
        raise ValueError("완료된 실험만 전송할 수 있습니다.")
    config = record["config"]
    if (config.get("target_url") != "http://127.0.0.1:8731/vulnerable"
            or config.get("comparison_url") != "http://127.0.0.1:8731/isolated" or config.get("fault_mode") != "demo"):
        raise ValueError("이 검증 명령은 전용 합성 예제 실험만 지원합니다.")
    agent = record["agent"]
    trace = agent["trace"]
    rows = trace["runs"]
    source_root = str(UUID(trace["root_id"]))
    if not 1 <= len(rows) <= 32 or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("추적 개수 또는 ID가 올바르지 않습니다.")
    root = next(r for r in rows if r["id"] == source_root)
    if root["name"] != "burstguard.supervisor" or root.get("parent_run_id"):
        raise ValueError("감독 루트가 올바르지 않습니다.")
    nonce = str(uuid4())
    mapping = {r["id"]: str(uuid4()) for r in rows}
    cloud_root = mapping[source_root]
    outputs, projected = {}, []
    for r in rows:
        name = r["name"]
        if name not in ROLES or r["run_type"] != ROLES[name] or r.get("error") or not r.get("end_time"):
            raise ValueError("완료된 허용 역할의 추적만 전송할 수 있습니다.")
        if r["id"] != source_root and r.get("parent_run_id") != source_root:
            raise ValueError("감독에 연결되지 않은 하위 추적입니다.")
        start, end = [datetime.fromisoformat(r[k]) for k in ("start_time", "end_time")]
        if end < start:
            raise ValueError("추적 시간이 올바르지 않습니다.")
        out = r.get("outputs") or {}
        if out.get("status") != "completed":
            raise ValueError("실패한 역할을 완료 추적으로 전송할 수 없습니다.")
        clean_output = {"status": "completed", "verification_nonce": nonce}
        clean_input = {"synthetic_only": True}
        if r["id"] == source_root:
            clean_output.update(experiments=count(out.get("experiments")),
                validation="complete" if out.get("validation") == "complete" else "partial",
                llm_calls=count(agent["usage"].get("llm_calls")),
                input_tokens=count(agent["usage"].get("input_tokens")), output_tokens=count(agent["usage"].get("output_tokens")),
                recorded_mcp_calls=[c for c in agent["mcp_calls"] if c in MCP_CALLS])
        else:
            clean_input["attempt"] = count((r.get("inputs") or {}).get("attempt"))
            clean_output["llm_calls_completed_so_far"] = count(out.get("llm_calls"))
        parent = mapping.get(r.get("parent_run_id"))
        # Build dotted order from validated timestamps/IDs; never forward free-form trace fields.
        root_order = datetime.fromisoformat(root["start_time"]).strftime("%Y%m%dT%H%M%S%fZ") + cloud_root
        order = root_order if parent is None else root_order + "." + start.strftime("%Y%m%dT%H%M%S%fZ") + mapping[r["id"]]
        projected.append({"id": mapping[r["id"]], "name": name, "run_type": ROLES[name], "inputs": clean_input,
            "outputs": clean_output, "parent_run_id": parent, "trace_id": cloud_root,
            "dotted_order": order, "start_time": start, "end_time": end,
            "extra": {"metadata": {"synthetic_only": True, "verification_nonce": nonce,
                "evidence_type": "actual-main-run-metadata-export"}}, "tags": ["burstguard", "synthetic-only", "metadata-export"]})
        outputs[mapping[r["id"]]] = clean_output
    return {"root_id": cloud_root, "source_root_id": source_root, "nonce": nonce,
        "runs": sorted(projected, key=lambda r: r["parent_run_id"] is not None), "outputs": outputs}


def verify_readback(payload, received):
    expected = {r["id"]: r for r in payload["runs"]}
    seen = {str(r.id): r for r in received}
    missing, invalid = [], []
    for run_id, row in expected.items():
        r = seen.get(run_id)
        if r is None:
            missing.append(run_id)
            continue
        parent = str(r.parent_run_id) if r.parent_run_id else None
        if (r.name != row["name"] or r.run_type != row["run_type"] or parent != row["parent_run_id"]
                or str(r.trace_id) != payload["root_id"] or not r.end_time or r.error
                or abs((r.start_time - row["start_time"]).total_seconds()) > .001
                or abs((r.end_time - row["end_time"]).total_seconds()) > .001
                or r.inputs != row["inputs"] or r.outputs != payload["outputs"][run_id]):
            invalid.append(run_id)
    return {"verified": not missing and not invalid, "expected": len(expected),
        "received": len(set(seen) & set(expected)), "missing_ids": missing, "invalid_ids": invalid}


def upload_and_verify(payload, api_key, project="burstguard-cloud-verification", endpoint="https://api.smith.langchain.com",
                      workspace_id=None, client_factory=Client, waits=(0, 1, 2, 4, 8)):
    client = client_factory(api_url=endpoint, api_key=api_key, workspace_id=workspace_id,
        auto_batch_tracing=False, timeout_ms=10000, omit_traced_runtime_info=True)
    received, root, result = {}, None, {}
    try:
        for run in payload["runs"]:
            client.create_run(project_name=project, **run)
        for delay in waits:
            if delay:
                time.sleep(delay)
            for row in payload["runs"]:
                if row["id"] in received:
                    continue
                try:
                    value = client.read_run(row["id"])
                except LangSmithNotFoundError:
                    continue
                # Ingestion may expose a span before its final outputs. Recheck unfinished data.
                if value.end_time and value.outputs == payload["outputs"][row["id"]]:
                    received[row["id"]] = value
            result = verify_readback(payload, list(received.values()))
            if result["verified"]:
                break
        root = received.get(payload["root_id"])
        return {"status": "verified" if result.get("verified") else "incomplete", "synthetic_only": True,
            "delivery_mode": "completed_run_metadata_export", "project": project, "cloud_root_id": payload["root_id"],
            "source_root_id": payload["source_root_id"], "verification_nonce": payload["nonce"],
            "url": getattr(root, "url", None), "readback": result,
            "completed_llm_spans": sum(r.run_type == "llm" and bool(r.end_time) and not r.error for r in received.values()),
            "completed_tool_spans": sum(r.run_type == "tool" and bool(r.end_time) and not r.error for r in received.values()),
            "span_names": sorted({r.name for r in received.values()}),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope": "실제 합성 메인 실행의 역할·완료 상태·시간·집계 메타데이터를 내보내고 모든 ID·부모 관계·입출력을 클라우드에서 대조했습니다. 첨부·프롬프트 원문이나 인증은 전송하지 않습니다."}
    finally:
        client.close()
