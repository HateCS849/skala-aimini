"""Explicit metadata-only LangSmith export; defaults to a local payload preview."""
import argparse
import json
import logging
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from burstguard.cloud_trace import metadata_payload, upload_and_verify
from burstguard.storage import Records


def main(args):
    # SDK errors can contain credential fragments: never print their raw messages.
    logging.getLogger("langsmith").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message="read_run.*", category=DeprecationWarning)
    store = Records()
    record = store.get(args.record_id)
    if record is None:
        raise ValueError("실험 기록이 없습니다.")
    payload = metadata_payload(record)
    result = {"status": "preview", "source_experiment_id": record["id"], "synthetic_only": True,
        "spans": len(payload["runs"]), "fields": ["role", "timestamps", "completion", "aggregate_counts", "parent_ids"],
        "excluded": ["attachments", "prompts", "response_bodies", "request_bodies", "credentials", "project_names"]}
    if not args.allow_synthetic_external_trace:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    settings = dotenv_values(args.credentials_file)
    key = settings.get("LANGSMITH_API_KEY") or settings.get("LANGCHAIN_API_KEY")
    if not key:
        raise ValueError("연결 설정 파일에 LangSmith 키가 없습니다.")
    endpoint = settings.get("LANGSMITH_ENDPOINT") or settings.get("LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com"
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in {"api.smith.langchain.com", "eu.api.smith.langchain.com", "api.us.smith.langchain.com"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("공식 LangSmith HTTPS API 주소가 필요합니다.")
    try:
        result = upload_and_verify(payload, key, project="burstguard-cloud-verification", endpoint=endpoint,
            workspace_id=settings.get("LANGSMITH_WORKSPACE_ID"))
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__, "synthetic_only": True,
            "source_experiment_id": record["id"], "message": "외부 전송 또는 수신 대조에 실패했습니다. 키·응답 본문은 기록하지 않습니다."}
    result["source_experiment_id"] = record["id"]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["status"] == "verified":
        trace = record["agent"]["trace"]
        trace.update(cloud_verified=True, delivery="metadata_export_verified", cloud=result)
        store.save(record)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id")
    parser.add_argument("--allow-synthetic-external-trace", action="store_true")
    parser.add_argument("--credentials-file", type=Path, default=Path("../crashcustomer/.env"))
    parser.add_argument("--output", type=Path, default=Path("docs/langsmith-cloud-receipt-20261022.json"))
    try:
        raise SystemExit(main(parser.parse_args()))
    except (ValueError, KeyError, StopIteration) as error:
        print(json.dumps({"status": "invalid_configuration_or_record", "error_type": type(error).__name__}))
        raise SystemExit(2)
