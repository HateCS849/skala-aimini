from .models import ExperimentSpec, IntegritySpec, Journey, RestartSpec, SoakSpec, Step, demo_spec


def all_presets():
    base = demo_spec().model_dump()
    result = []

    def add(key, label, description, patch):
        spec = ExperimentSpec.model_validate(base | patch)
        result.append({"id": key, "label": label, "description": description, "spec": spec.model_dump()})

    add("pool", "로그인 지연 · DB 풀 고갈", "외부 인증이 느려질 때 목록·검색으로 장애가 번지는지 비교합니다.", {})
    inventory = IntegritySpec(enabled=True, setup=Step(name="독립 재고 준비", method="POST", path="/orders/reset", body={"scope": "{{experiment_id}}", "stock": 5}))
    add("integrity", "동시 주문 · 중복 처리 · 재고", "재고 5개에 동시 주문과 동일 키 반복 요청을 보내 실제 저장 결과를 확인합니다.",
        {"name": "동시 주문 · 중복 처리 · 재고 정합성", "mode": "integrity", "external_delay_seconds": 0,
         "users": 20, "integrity": inventory.model_dump(), "journeys": [Journey(name="정상 조회", steps=[Step(path="/foods", expect_json="items")]).model_dump()], "protected_step": None})
    login = [Journey(name="로그인", steps=[Step(name="로그인", method="POST", path="/login", body={"user": "test-{{vu}}"})]).model_dump()]
    common = {"external_delay_seconds": 0, "users": 6, "fault_seconds": 4, "journeys": login, "think_ms": 200}
    add("retry", "외부 429 · 재시도 증폭", "실제 외부 호출 수를 세어 논리 요청 대비 호출 증폭을 비교합니다.", common | {"name": "외부 요청 제한 · 재시도 폭증", "external_status": 429})
    add("malformed", "외부 응답 형식 오류", "외부 제공자가 잘못된 JSON을 반환할 때 예외·재시도·다른 기능 영향을 확인합니다.", common | {"name": "외부 응답 형식 변경", "faults": {"external_behavior": "malformed"}})
    add("disconnect", "외부 연결 중간 끊김", "실제 HTTP 응답 스트림을 끊어 연결 오류 대응과 재시도를 확인합니다.", common | {"name": "외부 연결 중간 끊김", "faults": {"external_behavior": "disconnect"}})
    add("cache", "Redis 연결 장애 · DB 대체", "별도 실제 Redis 연결을 끊고 캐시 실패가 사용자 요청·DB 읽기에 미치는 영향을 봅니다.",
        {"name": "Redis 연결 장애 · DB 대체 경로", "external_delay_seconds": 0, "users": 10, "faults": {"cache_unavailable": True},
         "journeys": [Journey(name="캐시 목록", steps=[Step(path="/cached-foods", expect_json="items")]).model_dump()],
         "protected_step": Step(name="보호할 캐시 목록", path="/cached-foods", expect_json="items").model_dump()})
    add("db-delay", "DB 응답 지연 · 연결 점유", "조회 중 DB 연결을 유지한 채 지연해 풀 대기와 응답 지연을 확인합니다.",
        {"name": "DB 응답 지연 · 풀 대기", "external_delay_seconds": 0, "faults": {"db_delay_ms": 1500}, "fault_seconds": 4,
         "journeys": [Journey(name="음식 목록", steps=[Step(path="/foods", expect_json="items")]).model_dump()]})
    add("db-lock", "실제 DB 쓰기 잠금", "SQLite 쓰기 잠금을 획득한 동안 쓰기 요청이 실패하는지 계측합니다.",
        {"name": "DB 쓰기 잠금 · 경합", "external_delay_seconds": 0, "users": 4, "fault_seconds": 4, "faults": {"db_lock_seconds": 3},
         "journeys": [Journey(name="쓰기", steps=[Step(method="POST", path="/db/write", expect_json="saved")]).model_dump()]})
    add("restart", "프로세스 재시작 · 데이터 보존", "관리 중인 예제 프로세스를 실제로 재시작하고 기준 데이터 보존과 요청 실패를 확인합니다.",
        {"name": "프로세스 재시작 · 데이터 보존", "external_delay_seconds": 0, "users": 6, "fault_seconds": 6,
         "setup_steps": [Step(name="재시작 기준 재고", method="POST", path="/orders/reset", body={"scope": "{{experiment_id}}", "stock": 5}).model_dump(),
                         Step(name="기준 주문", method="POST", path="/orders", body={"scope": "{{experiment_id}}", "key": "seed", "quantity": 1}).model_dump()],
         "journeys": [Journey(name="목록", steps=[Step(path="/foods", expect_json="items")]).model_dump()],
         "restart": RestartSpec(enabled=True, snapshot=Step(path="/orders/state?scope={{experiment_id}}"), at_seconds=2).model_dump()})
    add("soak", "장시간 안정성 · 자원 증가", "3분 기본 부하로 현재 RSS·힙·파일/소켓·스레드의 지속 증가 징후를 확인합니다. 최대 1시간까지 설정할 수 있습니다.",
        {"name": "장시간 안정성 · 자원 증가", "external_delay_seconds": 0, "users": 4, "fault_seconds": 180, "think_ms": 1000,
         "soak": SoakSpec(enabled=True).model_dump(), "journeys": [Journey(name="페이지 처리", steps=[Step(path="/leaky", expect_values={"ok": True})]).model_dump()], "protected_step": None})
    return result
