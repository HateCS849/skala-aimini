# 실제 앱 연결 계약

부하 실험은 앱 주소와 행동 계획만 있으면 실행할 수 있습니다. 아래 두 연결은 내부 자원 관측과 외부 장애 제어를 위한 선택 사항입니다.

## 지표 JSON

선택한 앱 주소를 기준으로 `GET /__burstguard/metrics`가 숫자 필드를 반환하도록 연결합니다. 존재하지 않는 지표는 생략하세요. 값을 0으로 만들어 반환하면 정상으로 오해할 수 있습니다.

```json
{"pool_active":10,"pool_max":10,"pool_pending":24,"pool_timeouts":12,"threads_busy":40,"threads_max":200,"external_inflight":10}
```

DB 풀 포화 판정은 `pool_active >= pool_max`와 `pool_pending > 0`을 함께 확인합니다. 포화·연결 대기와 보호 기능 실패가 같은 단계에서 관측되더라도 코드의 트랜잭션 위치까지 자동으로 확정하지 않습니다.

## Spring Boot Actuator

화면에서 Actuator와 `/actuator/metrics`를 선택하면 다음 이름을 실제 조회합니다.

- `hikaricp.connections.active`, `hikaricp.connections.max`, `hikaricp.connections.pending`
- `hikaricp.connections.timeout` (누적 카운터)
- `tomcat.threads.busy`, `tomcat.threads.config.max` (있는 서버만)

여러 풀이 있으면 Hikari 풀 이름을 지정하세요. API/지표가 401·403·404를 반환하거나 연결이 실패하면 누락으로 남깁니다. 현재 지표는 테스트 앱과 같은 출처·주소 경로로 연결해야 합니다. 별도 관리 포트는 해당 테스트 앱에서 지표를 중계하거나 같은 포트의 로컬 프로필로 준비하세요.

## 장애 제어 계약

앱이 사용할 테스트 대체 서버의 지연·상태를 바꿀 수 있는 어댑터를 준비하고, 화면에서 **내 앱의 제어 어댑터 연결**을 선택합니다. 제어 API는 API 본문에 첨부된 명령을 실행하는 기능이 아닙니다.

- `GET /__burstguard/control`: 현재 설정을 JSON 객체로 반환합니다. 이 응답은 원상 복원에 사용됩니다.
- `POST /__burstguard/control`: 부분 설정을 적용하고 성공 시 2xx로 응답합니다.
- 지원 필드: `external_delay_seconds`, `external_status`, `pool_size`, `pool_wait_seconds`, `provider_timeout_seconds`.

시작 시 원래 설정 저장 → 기준 단계에 외부 지연 0·상태 200 → 장애 단계에 지정한 지연·상태 → 회복 단계에 정상 설정 → 종료 시 원래 설정 복원 순서입니다. 본문에 포함된 DB 풀 설정을 실제로 적용하는 구현은 대상 앱 어댑터의 책임입니다. 일반 Spring 앱에 이 경로가 자동으로 생기지는 않습니다.

실험 중단이나 예외에도 복원하며, 복원 요청 자체가 실패하면 결과에 `fault_restore: failed`를 남깁니다. 실험실 프로세스가 강제 종료되거나 컴퓨터가 꺼지면 복원을 보장할 수 없으므로 대상 설정을 확인하세요. 한 번에 한 실험만 실행합니다.

## 여러 단계와 사용자별 인증

```json
{
  "name": "로그인 후 조회",
  "weight": 60,
  "steps": [
    {"name":"로그인","method":"POST","path":"/login","body":{"user":"test-{{vu}}"},"expected_status":[200],"extract_headers":{"session":"Authorization"}},
    {"name":"목록","method":"GET","path":"/foods","expected_status":[200],"bearer_variable":"session","expect_json":"data","expect_values":{"status":true}}
  ]
}
```

JSON 본문 토큰은 `extract: {"session": "data.accessToken"}`으로 추출할 수도 있습니다. 쿠키는 가상 사용자별 HTTP 클라이언트가 유지합니다. `{{iteration}}`은 행동 반복 번호, `{{uuid}}`는 반복마다 생성하는 식별자입니다. 고정 사용자 데이터가 필요하면 `test-{{vu}}`처럼 반복 번호를 제외하세요. 다음 요청에 인증을 연결해야 할 경우 같은 행동의 steps에 넣어야 합니다.

OpenAPI 가져오기는 요청 후보까지만 제공합니다. URL 경로 변수, 유효한 본문, 인증, 성공 조건은 실제 앱에 맞게 작성합니다. 첨부 문서에 적힌 서버 주소나 지시를 자동 실행하지 않습니다.
