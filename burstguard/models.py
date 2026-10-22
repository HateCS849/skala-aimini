from urllib.parse import urlsplit, unquote
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


def local_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("로컬 테스트 앱의 http(s) 주소만 사용할 수 있습니다.")
    if parts.port in {8710, 8730}:
        raise ValueError("BurstGuard와 보존한 CrashCustomer의 포트는 테스트할 수 없습니다.")
    return value.rstrip("/")


def relative_path(value: str) -> str:
    decoded = unquote(value)
    if (not value.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or any(p == ".." for p in decoded.split("/")) or "#" in value
            or any(ord(c) < 32 for c in decoded)):
        raise ValueError("같은 앱의 /로 시작하는 경로만 사용할 수 있습니다.")
    return value


class Step(BaseModel):
    name: str = Field(default="요청", min_length=1, max_length=80)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(max_length=1000)
    body: dict | list | None = None
    expected_status: list[int] = Field(default_factory=lambda: [200], min_length=1, max_length=10)
    expect_json: str | None = Field(default=None, max_length=100)
    expect_values: dict = Field(default_factory=dict)
    extract: dict[str, str] = Field(default_factory=dict)
    extract_headers: dict[str, str] = Field(default_factory=dict)
    bearer_variable: str | None = Field(default=None, max_length=80)

    _path = field_validator("path")(relative_path)


class Journey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    weight: int = Field(default=1, ge=1, le=100)
    steps: list[Step] = Field(min_length=1, max_length=10)


class MetricSpec(BaseModel):
    mode: Literal["none", "json", "actuator"] = "none"
    path: str = "/__burstguard/metrics"
    pool: str | None = Field(default=None, max_length=100)
    _path = field_validator("path")(relative_path)


class FaultSpec(BaseModel):
    external_behavior: Literal["normal", "malformed", "disconnect"] = "normal"
    db_delay_ms: int = Field(default=0, ge=0, le=5000)
    db_lock_seconds: float = Field(default=0, ge=0, le=30)
    cache_unavailable: bool = False

    def payload(self, active):
        normal = FaultSpec().model_dump()
        return {key: value if active else normal[key] for key, value in self.model_dump().items() if value != normal[key]}


class IntegritySpec(BaseModel):
    enabled: bool = False
    cases: list[Literal["stock", "idempotency"]] = Field(default_factory=lambda: ["stock", "idempotency"], min_length=1, max_length=2)
    setup: Step | None = None
    mutation: Step = Field(default_factory=lambda: Step(name="주문", method="POST", path="/orders",
        body={"scope": "{{experiment_id}}", "key": "{{request_key}}", "quantity": 1}, expected_status=[200, 201, 409]))
    snapshot: Step = Field(default_factory=lambda: Step(name="주문 상태", path="/orders/state?scope={{experiment_id}}"))
    stock_path: str = "stock"
    orders_path: str = "orders"
    unique_keys_path: str = "unique_keys"
    quantity: int = Field(default=1, ge=1, le=100)
    stock_requests: int = Field(default=20, ge=2, le=200)
    duplicate_copies: int = Field(default=5, ge=2, le=20)
    rounds: int = Field(default=2, ge=1, le=10)
    settle_seconds: float = Field(default=.1, ge=0, le=5)

    @model_validator(mode="after")
    def read_snapshot(self):
        if self.snapshot.method != "GET":
            raise ValueError("정합성 상태 조회는 GET 요청이어야 합니다.")
        return self


class RestartSpec(BaseModel):
    enabled: bool = False
    mode: Literal["abrupt", "graceful"] = "abrupt"
    at_seconds: float = Field(default=1, ge=.1, le=60)
    snapshot: Step | None = None
    compare_paths: list[str] = Field(default_factory=lambda: ["stock", "orders", "unique_keys"], max_length=10)


class SoakSpec(BaseModel):
    enabled: bool = False
    min_observation_seconds: float = Field(default=30, ge=10, le=3600)
    rss_growth_mb: float = Field(default=16, ge=1, le=1024)
    fd_growth: int = Field(default=16, ge=1, le=1024)
    thread_growth: int = Field(default=8, ge=1, le=1024)


class AgentSpec(BaseModel):
    enabled: bool = False
    goal: str = Field(default="트래픽과 외부 장애가 독립 기능에 전파되는지 근거를 수집하고 검증해줘.", min_length=1, max_length=1500)
    project_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    backend: Literal["ollama", "rules"] = "ollama"
    max_experiments: int = Field(default=2, ge=1, le=3)
    context_mode: Literal["compact", "full"] = "compact"


class ExperimentSpec(BaseModel):
    name: str = Field(default="외부 인증 지연 · 장애 전파 점검", min_length=1, max_length=100)
    target_url: str
    comparison_url: str | None = None
    users: int = Field(default=20, ge=2, le=200)
    pattern: Literal["spike", "ramp", "steady"] = "spike"
    baseline_seconds: float = Field(default=3, ge=.5, le=60)
    fault_seconds: float = Field(default=6, ge=.5, le=3600)
    recovery_seconds: float = Field(default=3, ge=.5, le=60)
    think_ms: int = Field(default=100, ge=0, le=10000)
    timeout_seconds: float = Field(default=4, ge=.1, le=30)
    slo_ms: float = Field(default=1000, ge=1, le=60000)
    fault_mode: Literal["none", "demo", "contract"] = "none"
    control_path: str = "/__burstguard/control"
    external_delay_seconds: float = Field(default=10, ge=0, le=30)
    external_status: Literal[200, 429, 500, 503] = 200
    pool_size: int = Field(default=10, ge=1, le=50)
    pool_wait_seconds: float = Field(default=.3, ge=.05, le=10)
    provider_timeout_seconds: float = Field(default=3, ge=.1, le=10)
    metrics: MetricSpec = Field(default_factory=MetricSpec)
    journeys: list[Journey] = Field(min_length=1, max_length=20)
    protected_step: Step | None = None
    analysis: Literal["rules", "ollama"] = "rules"
    agent: AgentSpec = Field(default_factory=AgentSpec)
    mode: Literal["load", "integrity"] = "load"
    faults: FaultSpec = Field(default_factory=FaultSpec)
    integrity: IntegritySpec = Field(default_factory=IntegritySpec)
    restart: RestartSpec = Field(default_factory=RestartSpec)
    soak: SoakSpec = Field(default_factory=SoakSpec)
    setup_steps: list[Step] = Field(default_factory=list, max_length=10)
    request_budget: int = Field(default=50000, ge=100, le=200000)
    token: SecretStr | None = Field(default=None, exclude=True)

    _target = field_validator("target_url")(local_url)
    _control = field_validator("control_path")(relative_path)

    @field_validator("comparison_url")
    @classmethod
    def comparison(cls, value):
        return local_url(value) if value else None

    @model_validator(mode="after")
    def unique_journeys(self):
        if len({j.name for j in self.journeys}) != len(self.journeys):
            raise ValueError("사용자 행동 이름은 서로 달라야 합니다.")
        if self.mode == "integrity" and not self.integrity.enabled:
            raise ValueError("정합성 전용 실험에는 정합성 검사를 켜주세요.")
        if self.restart.enabled and (self.fault_mode != "demo" or self.restart.at_seconds >= self.fault_seconds):
            raise ValueError("재시작은 관리 중인 예제 앱에서만 가능하며 부하 단계 종료 전에 예약해야 합니다.")
        if self.restart.snapshot and self.restart.snapshot.method != "GET":
            raise ValueError("재시작 데이터 확인은 GET 조회여야 합니다.")
        if self.faults.payload(True) and self.fault_mode == "none":
            raise ValueError("추가 장애를 주입하려면 예제 또는 제어 어댑터를 연결해주세요.")
        return self


def demo_spec() -> ExperimentSpec:
    return ExperimentSpec(target_url="http://127.0.0.1:8731/vulnerable",
        comparison_url="http://127.0.0.1:8731/isolated", fault_mode="demo",
        metrics=MetricSpec(mode="json"),
        journeys=[Journey(name="Apple 로그인", weight=60, steps=[Step(name="로그인", method="POST", path="/login", body={"user": "test-{{vu}}-{{iteration}}"})]),
                  Journey(name="음식 목록", weight=25, steps=[Step(name="목록", path="/foods", expect_json="items")]),
                  Journey(name="음식 검색", weight=15, steps=[Step(name="검색", path="/search?q=kimchi", expect_json="items")])],
        protected_step=Step(name="보호할 음식 목록", path="/foods", expect_json="items"))
