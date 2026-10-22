import asyncio
import json
import math
import random
import time
from collections import Counter, defaultdict
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph

from .metrics import MetricsReader
from .storage import now

MAX_REQUESTS = 50000


def render(value, variables):
    if isinstance(value, str):
        if value.startswith("{{") and value.endswith("}}") and value[2:-2] in variables:
            return variables[value[2:-2]]
        for key, item in variables.items():
            value = value.replace("{{" + key + "}}", str(item))
        if "{{" in value:
            raise ValueError("요청에 아직 정의되지 않은 변수가 있습니다.")
        return value
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    return value


def lookup(body, path):
    for key in path.split("."):
        body = body[int(key)] if isinstance(body, list) else body[key]
    return body


def percentile(values, fraction):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 2) if values else None


def aggregate(samples, elapsed):
    latencies = [s["ms"] for s in samples]
    failed = sum(not s["ok"] for s in samples)
    return {"requests": len(samples), "failures": failed, "error_rate": round(failed / len(samples), 4) if samples else None,
            "p50_ms": percentile(latencies, .5), "p95_ms": percentile(latencies, .95),
            "rps": round(len(samples) / elapsed, 2) if elapsed else 0,
            "statuses": dict(Counter(str(s["status"]) for s in samples)),
            "functional_failures": sum(s["reason"] == "json_assertion" for s in samples),
            "timeouts": sum(s["status"] == "timeout" for s in samples)}


class Runner:
    def __init__(self, records, client_factory=None, fixture=None, supervisor_factory=None):
        self.records = records
        self.tasks = {}
        self.client_factory = client_factory or httpx.AsyncClient
        self.fixture = fixture
        self.supervisor_factory = supervisor_factory

    def client(self, url, spec, *, runtime_token=True):
        headers = {"User-Agent": "BurstGuard/0.1 local-test"}
        if runtime_token and spec.token:
            headers["Authorization"] = "Bearer " + spec.token.get_secret_value()
        return self.client_factory(base_url=url.rstrip("/") + "/", timeout=spec.timeout_seconds,
                                   follow_redirects=False, trust_env=False, headers=headers)

    def emit(self, record, stage, message):
        record["stage"] = stage
        record["events"].append({"time": now(), "stage": stage, "message": message})
        self.records.save(record)

    def start(self, spec):
        if any(not task.done() for task in self.tasks.values()):
            raise ValueError("이미 실행 중인 실험이 있습니다. 완료 또는 중단 후 시작해주세요.")
        record = self.records.create(spec)
        self.tasks[record["id"]] = asyncio.create_task(self.execute(record, spec))
        return record

    async def shutdown(self):
        active = [t for t in self.tasks.values() if not t.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def request_step(self, client, step, variables):
        start = time.perf_counter()
        try:
            # Resolved paths are checked again: extracted variables cannot redirect a request.
            from .models import relative_path
            path = relative_path(render(step.path, variables))
            headers = None
            if step.bearer_variable:
                if step.bearer_variable not in variables:
                    raise ValueError("사용자별 인증 변수를 이전 단계에서 추출해야 합니다.")
                bearer = str(variables[step.bearer_variable]).removeprefix("Bearer ")
                headers = {"Authorization": "Bearer " + bearer}
            response = await client.request(step.method, path.lstrip("/"), headers=headers,
                                            json=render(step.body, variables) if step.body is not None else None)
            ok = response.status_code in step.expected_status
            reason = "" if ok else "unexpected_status"
            if ok and (step.expect_json or step.expect_values or step.extract or step.extract_headers):
                try:
                    body = response.json() if (step.expect_json or step.expect_values or step.extract) else None
                    if step.expect_json:
                        lookup(body, step.expect_json)
                    for pointer, expected in step.expect_values.items():
                        if lookup(body, pointer) != expected:
                            raise ValueError("Response value mismatch")
                    for name, pointer in step.extract.items():
                        variables[name] = lookup(body, pointer)
                    for name, header in step.extract_headers.items():
                        variables[name] = response.headers[header]
                except (ValueError, KeyError, IndexError, TypeError):
                    ok, reason = False, "json_assertion"
            return {"ok": ok, "status": response.status_code, "reason": reason,
                    "ms": round((time.perf_counter() - start) * 1000, 2)}
        except httpx.TimeoutException:
            return {"ok": False, "status": "timeout", "reason": "request_timeout", "ms": round((time.perf_counter() - start) * 1000, 2)}
        except httpx.HTTPError:
            return {"ok": False, "status": "connection_error", "reason": "transport", "ms": round((time.perf_counter() - start) * 1000, 2)}

    async def preflight(self, target, spec):
        self.emit(target["record"], "preflight", f"{target['label']} · 행동별 정상 응답과 JSON 조건을 확인합니다.")
        async with self.client(target["url"], spec) as client:
            for journey in spec.journeys:
                variables = {"vu": "preflight", "iteration": 0, "uuid": uuid4().hex, "experiment_id": target["record"]["id"]}
                for step in journey.steps:
                    result = await self.request_step(client, step, variables)
                    if not result["ok"]:
                        raise ValueError(f"{target['label']} 사전 확인 실패: {journey.name}/{step.name} ({result['status']}, {result['reason']}). 인증·경로·정상 응답 조건을 확인하세요.")
            if spec.protected_step:
                result = await self.request_step(client, spec.protected_step, {"vu": "canary", "iteration": 0, "uuid": uuid4().hex, "experiment_id": target["record"]["id"]})
                if not result["ok"]:
                    raise ValueError(f"보호 기능 사전 확인 실패 ({result['status']})")

    async def control(self, client, path, values):
        response = await client.post(path.lstrip("/"), json=values)
        response.raise_for_status()

    async def phase(self, record, target, spec, name, seconds):
        start = time.monotonic()
        end = start + seconds
        samples, instruments = [], []
        issued = 0
        groups = defaultdict(list)
        metrics = MetricsReader(None, spec.metrics)
        result = {"name": name, "duration_seconds": seconds, "status": "running", "metrics": instruments, "series": [], "by_journey": {}}
        target["phases"].append(result)
        phase_users = min(spec.users, 2) if name in {"baseline", "recovery"} and spec.pattern != "steady" else spec.users
        self.emit(record, name, f"{target['label']} · {seconds:g}초 · 동시 사용자 최대 {phase_users}명")
        sample_interval = max(.25 if spec.metrics.mode == "json" else 1, seconds / 1800)

        async def sample_metrics():
            async with self.client(target["url"], spec) as client:
                metrics.client = client
                while time.monotonic() < end:
                    point = await metrics.read()
                    point["second"] = round(time.monotonic() - start, 2)
                    instruments.append(point)
                    # Flush progress independently of requests so a blocked service still produces evidence.
                    result.update(total=aggregate(samples, max(.001, time.monotonic() - start)),
                                  by_journey={k: aggregate(v, max(.001, time.monotonic() - start)) for k, v in groups.items()})
                    result["series"] = self.series(samples)
                    self.records.save(record)
                    await asyncio.sleep(sample_interval)

        async def restart():
            if not (name == "fault" and spec.restart.enabled):
                return
            from .integrity import read_numbers
            await asyncio.sleep(spec.restart.at_seconds)
            event = {"mode": spec.restart.mode, "data_status": "unverified"}
            target.setdefault("restarts", []).append(event)
            values = {"experiment_id": record["id"], "vu": "restart", "iteration": 0, "uuid": uuid4().hex}
            async with self.client(target["url"], spec) as client:
                before = None
                if spec.restart.snapshot:
                    before = await read_numbers(client, spec.restart.snapshot, values, {p: p for p in spec.restart.compare_paths})
                self.emit(record, "restart", f"{target['label']} · 소유한 예제 프로세스를 {spec.restart.mode} 방식으로 재시작합니다.")
                event.update(await self.fixture.restart(spec.restart.mode))
                if spec.fault_mode != "none":
                    await self.control(client, spec.control_path, self.normal_control(spec) | self.fault_control(spec))
                if spec.restart.snapshot and before is not None:
                    after = await read_numbers(client, spec.restart.snapshot, values, {p: p for p in spec.restart.compare_paths})
                    event.update(before=before, after=after)
                    if set(before) == set(after) == set(spec.restart.compare_paths) and before:
                        changed = [p for p in before if before[p] != after[p]]
                        event.update(data_status="failed" if changed else "passed", changed_paths=changed)
                self.records.save(record)

        async def worker(index, canary=False):
            nonlocal issued
            rng = random.Random(index)
            if name in {"baseline", "recovery"} and spec.pattern != "steady":
                if index >= min(spec.users, 2):
                    return
            elif spec.pattern == "ramp" and name == "fault":
                await asyncio.sleep(seconds * .65 * index / spec.users)
            iteration = 0
            # Each virtual user owns its own cookies and extracted session variables.
            async with self.client(target["url"], spec) as client:
                variables = {"vu": index, "iteration": iteration, "uuid": uuid4().hex, "experiment_id": record["id"]}
                while time.monotonic() < end and issued < spec.request_budget:
                    variables.update(iteration=iteration, uuid=uuid4().hex)
                    journey = rng.choices(spec.journeys, weights=[j.weight for j in spec.journeys])[0]
                    steps = [spec.protected_step] if canary else journey.steps
                    group = "보호 기능 (독립 관찰)" if canary else journey.name
                    for step in steps:
                        if issued >= spec.request_budget:
                            break
                        issued += 1
                        response = await self.request_step(client, step, variables)
                        response.update(second=round(time.monotonic() - start, 3), journey=group, step=step.name, canary=canary)
                        samples.append(response)
                        groups[group].append(response)
                        if not response["ok"]:
                            break
                    iteration += 1
                    await asyncio.sleep(max(.05 if canary else .001, spec.think_ms / 1000))

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(sample_metrics())
                tasks.create_task(restart())
                for index in range(spec.users):
                    tasks.create_task(worker(index, canary=bool(spec.protected_step and index == 0)))
            elapsed = time.monotonic() - start
            async with self.client(target["url"], spec) as client:
                final = await MetricsReader(client, spec.metrics).read()
                instruments.append({**final, "second": round(time.monotonic() - start, 2)})
            result.update(status="completed", actual_seconds=round(elapsed, 2), total=aggregate(samples, elapsed),
                          by_journey={k: aggregate(v, elapsed) for k, v in groups.items()}, series=self.series(samples),
                          request_budget_hit=issued >= spec.request_budget,
                          # Bounded evidence; aggregates above are computed over every request.
                          failures=[s for s in samples if not s["ok"]][:100], configured_users=spec.users,
                          active_users=min(spec.users, 2) if name in {"baseline", "recovery"} and spec.pattern != "steady" else spec.users)
        except BaseException:
            elapsed = time.monotonic() - start
            result.update(status="interrupted", actual_seconds=round(elapsed, 2), total=aggregate(samples, elapsed),
                          by_journey={k: aggregate(v, elapsed) for k, v in groups.items()}, series=self.series(samples))
            raise
        finally:
            self.records.save(record)

    @staticmethod
    def series(samples):
        groups = defaultdict(list)
        for sample in samples:
            groups[int(sample["second"])].append(sample)
        return [{"second": second, **aggregate(rows, 1)} for second, rows in sorted(groups.items())]

    @staticmethod
    def normal_control(spec):
        return {"external_delay_seconds": 0, "external_status": 200, "pool_size": spec.pool_size,
                "pool_wait_seconds": spec.pool_wait_seconds, "provider_timeout_seconds": spec.provider_timeout_seconds,
                **spec.faults.payload(False)}

    @staticmethod
    def fault_control(spec):
        return {"external_delay_seconds": spec.external_delay_seconds, "external_status": spec.external_status, **spec.faults.payload(True)}

    async def execute(self, record, spec):
        if spec.agent.enabled:
            from .agents import Supervisor
            supervisor = self.supervisor_factory() if self.supervisor_factory else Supervisor()
            await supervisor.run(self, record, spec)
        else:
            await self.execute_plain(record, spec)

    async def execute_plain(self, record, spec):
        from .analysis import analyze, ai_analysis
        start = time.monotonic()
        record["status"] = "running"
        try:
            urls = [("검증 대상", spec.target_url)]
            if spec.comparison_url:
                urls = [("변경 전", spec.target_url), ("변경 후", spec.comparison_url)]
            for label, url in urls:
                if spec.restart.enabled:
                    if not self.fixture or not self.fixture.owns_url(url):
                        raise ValueError("재시작은 이 BurstGuard가 실행한 예제 프로세스에만 사용할 수 있습니다.")
                target = {"label": label, "url": url, "phases": [], "fault_restore": "not_used"}
                record["targets"].append(target)
                previous = None
                async with self.client(url, spec) as control_client:
                    try:
                        if spec.fault_mode != "none":
                            self.emit(record, "control", f"{label} · 장애 제어 연결과 원래 설정을 확인합니다.")
                            response = await control_client.get(spec.control_path.lstrip("/"))
                            response.raise_for_status()
                            previous = response.json()
                            if not isinstance(previous, dict):
                                raise ValueError("장애 제어 GET은 복원 가능한 설정 객체를 반환해야 합니다.")
                            if spec.fault_mode == "demo" and previous.get("adapter") != "burstguard-demo-v1":
                                raise ValueError("예제 앱이 아닙니다. 실제 앱은 제어 어댑터 모드를 선택하세요.")
                            target["fault_restore"] = "pending"
                            normal = self.normal_control(spec)
                            from .models import FaultSpec
                            normal.update({k: v for k, v in FaultSpec().model_dump().items() if k in previous})
                            await self.control(control_client, spec.control_path, normal)

                        async def preflight(state):
                            await self.preflight({**target, "record": record}, spec)
                            variables = {"experiment_id": record["id"], "vu": "setup", "iteration": 0, "uuid": uuid4().hex}
                            for step in spec.setup_steps:
                                outcome = await self.request_step(control_client, step, variables)
                                if not outcome["ok"]:
                                    raise ValueError("실험용 데이터 준비에 실패했습니다.")
                            return state

                        async def baseline(state):
                            await self.phase(record, target, spec, "baseline", spec.baseline_seconds)
                            return state

                        async def fault(state):
                            if spec.fault_mode != "none":
                                await self.control(control_client, spec.control_path, self.fault_control(spec))
                            await self.phase(record, target, spec, "fault", spec.fault_seconds)
                            return state

                        async def recovery(state):
                            if spec.fault_mode != "none":
                                await self.control(control_client, spec.control_path, {"external_delay_seconds": 0, "external_status": 200, **spec.faults.payload(False)})
                            await self.phase(record, target, spec, "recovery", spec.recovery_seconds)
                            return state

                        async def integrity(state):
                            if spec.integrity.enabled:
                                from .integrity import run_integrity
                                await run_integrity(self, record, target, spec)
                            return state

                        graph = StateGraph(dict)
                        for name, node in (("preflight", preflight), ("baseline", baseline), ("fault", fault), ("recovery", recovery), ("integrity", integrity)):
                            graph.add_node(name, node)
                        sequence = [START, "preflight", "integrity", END] if spec.mode == "integrity" else [START, "preflight", "baseline", "fault", "recovery", "integrity", END]
                        for left, right in zip(sequence, sequence[1:]):
                            graph.add_edge(left, right)
                        await graph.compile().ainvoke({})
                    finally:
                        if previous is not None:
                            try:
                                # No shielded orphan task: finish restoring before releasing the client.
                                await self.control(control_client, spec.control_path, previous)
                                target["fault_restore"] = "ok"
                            except Exception as error:
                                target["fault_restore"] = "failed"
                                target["restore_error"] = type(error).__name__
                            self.records.save(record)
            self.emit(record, "analyze", "실측 요청·자원 지표를 대조해 장애 영향 범위를 판정합니다.")
            record["summary"] = analyze(record, spec)
            if spec.analysis == "ollama":
                self.emit(record, "ai", "로컬 Ollama가 익명 집계 지표와 판정 근거를 분석합니다.")
                record["ai"] = await ai_analysis(record)
            record["status"] = "completed"
        except asyncio.CancelledError:
            record.update(status="cancelled", error="사용자가 실험을 중단했습니다. 진행 중이던 서버 요청은 대상 앱의 타임아웃까지 남을 수 있습니다.")
        except Exception as error:
            while isinstance(error, BaseExceptionGroup) and error.exceptions:
                error = error.exceptions[0]
            # Requests/HTTP errors can contain auth-bearing URLs. Keep only a safe type; our validation messages are static.
            message = str(error)[:400] if isinstance(error, ValueError) else type(error).__name__
            if isinstance(error, httpx.HTTPStatusError):
                message = f"제어 어댑터 요청이 HTTP {error.response.status_code}로 실패했습니다. 제어 경로·인증·앱의 지원 여부를 확인하세요."
            elif isinstance(error, httpx.ConnectError):
                message = "테스트 앱에 연결할 수 없습니다. 앱 실행 상태와 대상 포트를 확인하세요."
            record.update(status="error", error=message)
        finally:
            record.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - start, 2))
            self.emit(record, record["status"], "실험 종료 · " + record["status"])
