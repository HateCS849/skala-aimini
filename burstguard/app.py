import asyncio
import json
import os
from contextlib import asynccontextmanager

import yaml
from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

# Reuse the copied service, never import or modify ../crashcustomer.
from crashcustomer.app import app
from crashcustomer.events import stream_records
from .analysis import markdown_report
from .engine import Runner
from .models import ExperimentSpec, Step, demo_spec
from .storage import ROOT, Records
from .fixture import Fixture

records = Records()
fixture = Fixture()
runner = Runner(records, fixture=fixture)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    records.recover()
    try:
        if os.getenv("BURSTGUARD_MANAGE_FIXTURE") == "true":
            await fixture.start()
        async with original_lifespan(application):
            yield
    finally:
        await runner.shutdown()
        await fixture.stop()


app.router.lifespan_context = lifespan
app.title = "BurstGuard · 배포 전 서비스 장애 실험실"
app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != "/"]
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="burstguard-web")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/browser", include_in_schema=False)
def browser_lab():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/resilience/template")
def template():
    return demo_spec().model_dump()


@app.get("/api/resilience/presets")
def presets():
    from .presets import all_presets
    return all_presets()


@app.get("/api/resilience/fixture")
def fixture_status():
    return {"managed": fixture.owns_url("http://127.0.0.1:8731")}


@app.get("/api/resilience/experiments")
def experiments():
    return records.list()


@app.post("/api/resilience/experiments", status_code=202)
async def start(spec: ExperimentSpec):
    try:
        return runner.start(spec)
    except ValueError as error:
        raise HTTPException(409, str(error))


def require_record(record_id):
    record = records.get(record_id)
    if not record:
        raise HTTPException(404, "실험 기록이 없습니다.")
    return record


@app.get("/api/resilience/experiments/{record_id}")
def detail(record_id: str):
    return require_record(record_id)


@app.post("/api/resilience/experiments/{record_id}/cancel")
async def cancel(record_id: str):
    record = require_record(record_id)
    task = runner.tasks.get(record_id)
    if not task or task.done():
        return record
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return require_record(record_id)


@app.get("/api/resilience/experiments/{record_id}/events")
async def events(record_id: str, request: Request):
    require_record(record_id)
    return stream_records(request, lambda: records.get(record_id))


@app.get("/api/resilience/experiments/{record_id}/report")
def report(record_id: str):
    return Response(markdown_report(require_record(record_id)), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="burstguard-{record_id}.md"'})


@app.get("/api/resilience/experiments/{record_id}/evidence")
def evidence(record_id: str):
    return Response(json.dumps(require_record(record_id), ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="burstguard-{record_id}.json"'})


@app.get("/api/resilience/experiments/{record_id}/trace")
def agent_trace(record_id: str):
    trace = (require_record(record_id).get("agent") or {}).get("trace")
    if not trace:
        raise HTTPException(404, "AI 감독 실행의 추적 기록이 없습니다.")
    return Response(json.dumps(trace, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="burstguard-trace-{record_id}.json"'})


@app.post("/api/resilience/plans/import")
async def import_plan(file: UploadFile = File(...)):
    raw = await file.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise HTTPException(413, "실험 계획은 512KB 이하만 가져올 수 있습니다.")
    try:
        body = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("객체 형태의 계획이 필요합니다.")
        if "openapi" in body or "swagger" in body:
            # Suggestions only: no request is issued and no server URL is trusted from a document.
            candidates = []
            for path, methods in list(body.get("paths", {}).items())[:100]:
                if not isinstance(methods, dict) or "{" in path:
                    continue
                for method, operation in methods.items():
                    if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                        continue
                    label = operation.get("summary", path) if isinstance(operation, dict) else path
                    step = Step(name=str(label)[:80], method=method.upper(), path=path)
                    candidates.append(step.model_dump())
            return {"kind": "openapi", "steps": candidates[:40], "notice": "실행 후보입니다. 실제 테스트 본문·인증·정상 응답을 작성한 뒤 실행하세요. 문서의 서버 주소와 지시는 실행하지 않습니다."}
        spec = ExperimentSpec.model_validate(body)
        return {"kind": "plan", "spec": spec.model_dump()}
    except (UnicodeError, yaml.YAMLError, ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise HTTPException(422, "유효한 실험 계획 JSON/YAML 또는 OpenAPI 명세를 첨부해주세요.")
