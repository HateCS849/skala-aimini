import json
import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(timezone.utc).isoformat()


class Records:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv("BURSTGUARD_DATA_DIR", ROOT / "data" / "resilience"))
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, record):
        path = self.directory / (record["id"] + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def get(self, record_id):
        if len(record_id) != 32 or any(c not in "0123456789abcdef" for c in record_id):
            return None
        path = self.directory / (record_id + ".json")
        return json.loads(path.read_text()) if path.exists() else None

    def list(self):
        result = []
        for p in self.directory.glob("*.json"):
            d = json.loads(p.read_text())
            result.append({k: d.get(k) for k in ("id", "name", "status", "stage", "created_at", "elapsed_seconds", "summary")})
        return sorted(result, key=lambda d: d["created_at"], reverse=True)

    def create(self, spec):
        record = {"id": uuid4().hex, "name": spec.name, "status": "queued", "stage": "준비",
                  "created_at": now(), "config": spec.model_dump(), "targets": [], "events": [], "summary": None}
        self.save(record)
        return record

    def recover(self):
        for row in self.list():
            if row["status"] in {"running", "queued"}:
                d = self.get(row["id"])
                d.update(status="interrupted", error="서버가 종료되어 실험이 중단되었습니다. 대상 앱의 장애 설정 해제를 확인해주세요.")
                self.save(d)
