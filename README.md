# BurstGuard

실제 로컬 앱에 사용자 행동과 트래픽을 재현하고, 외부 연동 장애가 다른 기능으로 번지는지 배포 전에 확인하는 실험 도구입니다.

2026-10-22: 새 메인 화면에 **AI 감독**을 통합했습니다. 결과 분석에서 `AI 감독 · 근거 검색 → 계획 → 실행 → 검증`을 선택하면 실제 MCP로 프로젝트 자료와 Runbook을 검색하고, 계획 AI와 검증 AI가 기존 부하 실행 작업자와 협력합니다. 지정한 대상·API·최대 인원을 유지하며 최대 1~3회 안에서 원래 실험과 대조 실험을 수행합니다. 실험별 근거 충분성, 후속 대조, AI 대체 여부, 로컬 추적을 화면과 보고서에서 확인할 수 있습니다.

- [PDF 평가 대비 현황](docs/PDF_READINESS_20261022.md)
- [새 메인 AI 실제 실행 증거](docs/agent-live-evidence-20261022.json)
- [새 메인 AI 입력 최적화 실측](docs/benchmark-resilience-agents-20261022.json)

LangSmith RunTree SDK 추적은 기본적으로 로컬 JSON에 저장합니다. 명시적으로 허용한 합성 실행의 메타데이터는 클라우드에 전송하고 **13/13개를 다시 조회해 수신 확인**했습니다. [클라우드 수신 증빙](docs/LANGSMITH_CLOUD_VERIFICATION_20261022.md)을 참조하세요. 일반 실험을 자동 전송하도록 설정한 것은 아닙니다.

기존 `../crashcustomer`를 복사해 만든 별도 프로그램입니다. 원본 소스·`.env`·실행 기록을 수정하지 않습니다. 새 복사본은 독립 가상환경과 데이터 폴더를 사용하며, 원본의 프로젝트 5개와 첨부 자료 8개만 읽기 전용 연결로 복사했습니다. 인증 키·기존 실행 이력은 가져오지 않았습니다.

## 실행 방법

### 1. 준비물

| 항목 | 필요 여부 | 비고 |
|---|---|---|
| Python 3.11 이상 | 필수 | |
| [uv](https://docs.astral.sh/uv/) | 필수 | 의존성 설치 (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| Ollama + `qwen3:8b` | 선택 | AI 감독·AI 분석을 쓸 때만. 없으면 규칙 기반으로 동작 |
| Docker | 선택 | Redis 장애 실험을 할 때만 |

### 2. 설치

```bash
git clone https://github.com/HateCS849/skala-aimini.git
cd skala-aimini
uv sync --extra dev --locked
cp .env.example .env   # 선택: Ollama 주소·모델, LangSmith 설정
```

브라우저 검사 화면(`/browser`)까지 쓰려면 Playwright 브라우저를 한 번 설치합니다.

```bash
.venv/bin/python -m playwright install chromium
```

### 3. 서버 실행

```bash
bash scripts/start_all.sh
```

실험실(8730)과 장애 재현 예제 앱(8731)이 함께 뜹니다. 종료는 `Ctrl-C`입니다. 8730~8732 포트가 이미 사용 중이면 기존 프로세스는 건드리지 않고 시작을 멈춥니다.

| 주소 | 내용 |
|---|---|
| http://127.0.0.1:8730/ | BurstGuard 실험실 (메인 화면) |
| http://127.0.0.1:8730/browser | 브라우저 검사·프로젝트 자료 |
| http://127.0.0.1:8730/docs | API 문서 (FastAPI) |
| http://127.0.0.1:8731/health | 장애 재현 예제 앱 |

실험실만 따로 실행하려면 `bash scripts/run.sh`를 사용합니다.

### 4. 선택 기능

**로컬 AI (AI 감독·AI 분석)**

```bash
ollama pull qwen3:8b
ollama serve
```

화면의 결과 분석에서 `AI 감독 · 근거 검색 → 계획 → 실행 → 검증`을 선택합니다. 모델과 주소는 `.env`의 `OLLAMA_MODEL`, `OLLAMA_BASE_URL`로 바꿉니다.

**Redis 장애 실험**

```bash
bash scripts/start_redis.sh
```

Docker의 `redis:7-alpine`을 `burstguard-fixture` 프로젝트와 127.0.0.1:16379 포트로 실행합니다. 기존 Redis를 재사용하거나 중지하지 않습니다. 중지할 때는 `docker compose -p burstguard-fixture -f examples/redis-compose.yaml down`을 사용합니다.

**LangSmith 추적**

`.env`에 `LANGSMITH_TRACING=true`와 `LANGSMITH_API_KEY`를 넣습니다. `.env`는 Git에 올라가지 않습니다. 기본값은 로컬 JSON 추적만 남깁니다.

### 5. 테스트

```bash
LANGSMITH_TRACING=false .venv/bin/python -m pytest -q
```

단위·통합 테스트 212개가 실행됩니다. 실제 서버나 Ollama 없이 가짜 모델·가짜 HTTP 클라이언트를 주입해서 돌아갑니다.

## 바로 확인하는 실험

1. 메인 화면의 **예제 불러오기**를 선택합니다.
2. 동시 사용자, 트래픽 패턴, 기준/부하/회복 시간, 외부 응답 지연, DB 풀 크기를 설정합니다.
3. **실험 시작**을 누릅니다. 기본 계획은 로그인 60 / 목록 25 / 검색 15의 상대 가중치입니다.
4. 변경 전후를 차례로 실행하고 실제 요청·풀 활성 연결·대기 수를 기록합니다.
5. 측정 대상 선택에서 변경 전/후를 전환하고, 사용자 행동별 결과와 판정 근거를 봅니다. Markdown 보고서와 JSON 실측 자료를 저장할 수 있습니다.

20명·풀 10개·외부 지연 10초로 먼저 실행해보세요. 보호 기능 관찰에 전체 인원 중 1명을 예약하므로 10명·풀 10개처럼 여유가 있는 조건에서는 풀 고갈이 발생하지 않을 수도 있습니다. 이것도 정상적인 실험 결과입니다.

예제의 변경 전 코드는 실제 SQLite 연결과 트랜잭션을 유지한 채 실제 HTTP 외부 인증 응답을 기다립니다. 변경 후는 외부 호출을 DB 연결 획득 전에 실행하고, 외부 타임아웃·크기가 제한된 비동기 오류 로그 큐와 파일 대체 기록을 사용합니다. 외부 제공자는 같은 예제 서버의 별도 HTTP 엔드포인트이며, 요청은 실제 네트워크를 왕복합니다. Spring/HikariCP나 실제 Hangari의 결과라고 주장하지 않습니다.

## 가능한 실험

| 항목 | 기능 |
|---|---|
| 부하 | 2~200명, 순간 급증·점진 증가·일정 부하, 단계별 시간과 사용자 대기 시간 |
| 행동 | GET/POST/PUT/PATCH/DELETE, 가중치, 여러 단계 순서 실행, 사용자별 쿠키와 변수 |
| 응답 검증 | 기대 상태 코드, 필수 JSON 경로, JSON 값 일치 조건 |
| 인증 | 실행 중에만 사용하는 Bearer 토큰, 응답 JSON/헤더에서 사용자별 세션 값 추출 |
| 장애 | 예제 또는 연결한 제어 어댑터를 통한 외부 지연·응답 코드 변경 |
| 지표 | 앱의 JSON 지표 또는 Spring Boot Actuator의 실제 HikariCP/스레드 지표 |
| 판정 | 오류율, P95 목표 초과, 응답 조건 불일치, 보호 기능 영향, 풀 포화·대기, 회복 단계 실패 |
| 비교 | 동일 조건으로 두 로컬 앱/버전을 순차 실행 |
| 자료 | JSON/YAML 실험 계획 가져오기·저장, OpenAPI 요청 후보 읽기 |
| 관측 | SSE 진행·타임라인·그래프·중단·장애 설정 복원·실행 기록 복구 |
| AI | 로컬 Ollama가 실측에 근거한 후보 중 후속 실험 우선순위 선택 |

화면의 **예제 시나리오**에서 다음 검사를 선택할 수 있습니다.

| 추가 시나리오 | 실제 검증 범위 |
|---|---|
| 동시 주문·중복 처리·재고 | 독립된 재고 5개에 동시 주문. 저장된 주문 수·남은 재고·고유 요청 키로 초과 판매와 중복 처리를 판정 |
| 외부 429·재시도 | 논리 로그인 수와 실제 외부 HTTP 시도 수를 비교해 3배 초과 증폭 표시 |
| 외부 형식 오류·연결 끊김 | 깨진 JSON, 실제 HTTP 스트림 중단에 대한 응답·예외·재시도 관측 |
| Redis 연결 장애 | 별도 실제 Redis 앞의 TCP 게이트를 끊어 캐시 실패·DB 대체·사용자 영향을 관측 |
| DB 연결 보유 지연 | 조회 중 연결 보유 시간을 늘려 지연과 풀 대기 관측. 실제 SQL 실행계획이 느려졌다는 의미는 아님 |
| DB 쓰기 잠금 | 실제 SQLite `BEGIN IMMEDIATE` 잠금 중 쓰기 실패와 잠금 충돌 카운터 관측 |
| 프로세스 재시작 | 소유한 예제 프로세스만 실제 종료·재기동. 부트 ID 변경과 기준 데이터 보존 비교 |
| 장시간 안정성 | 기본 3분·최대 1시간. 현재 RSS·힙·파일/소켓·스레드의 지속 증가 징후 탐색 |

정합성 전용 검사는 시간 기반 부하 단계를 생략하고 준비→동시 요청→전후 상태 조회를 반복합니다. 단순히 모든 요청을 거절하는 서버는 중복 처리 검사를 통과하지 않습니다. 상태 숫자가 없거나 초기 데이터가 부족하면 미확인으로 남깁니다. 각 테스트는 새 실행 ID의 namespace를 사용해 기존 주문을 초기화하지 않습니다.

재시작을 사용하려면 `scripts/start_all.sh`로 실행해야 합니다. 이 실행 경로에서는 실험실이 예제 프로세스를 직접 소유하고, 서버 종료 시 함께 정리합니다. 임의 PID·명령·앱·컨테이너를 종료하는 API는 없습니다. 실제 Hangari 재시작이나 실제 배포 롤링 업데이트 검증은 아직 연결하지 않았습니다.

장시간 검사는 준비된 예제의 메모리 증가를 관찰할 수 있습니다. 증가 징후는 누수 원인 확정과 구분합니다. 예제의 의도적인 메모리 보유는 96MiB로 제한했습니다. 파일/소켓·스레드 지표를 수집하지만 그 자원의 누수까지 실제 예제로 검출했다는 의미는 아닙니다. [추가 진단 가이드](docs/EXTENDED_DIAGNOSTICS.md)

계획 샘플은 `examples/resilience-plan.json`입니다. 예전 자료 첨부·브라우저 탐색·장애 주입·재현 기능은 `/browser`에 유지했습니다. 새 부하 기능은 `burstguard/`, 메인 화면은 `web/`에 있습니다.

## 내 앱 연결

앱을 로컬에서 먼저 실행한 뒤 주소와 실제 API 경로를 작성합니다. 소스 ZIP을 첨부해도 앱을 자동 실행하거나 DB 설정을 바꾸지는 않습니다. 로그인·DB·Redis 등 앱이 필요로 하는 의존성은 테스트 환경으로 준비해야 합니다.

- 단순 부하: **트래픽만 측정**을 선택하고 실제 사용자 행동을 작성합니다.
- 내부 원인 관측: **Actuator** 또는 JSON 지표를 연결합니다. 지표가 없으면 커넥션 고갈을 확정하지 않습니다.
- 외부 장애: 앱이 호출하는 테스트 대체 서버나 제어 어댑터가 필요합니다. 어떤 앱에서나 자동으로 외부 호출을 지연시키지는 않습니다.

구체적인 계약은 [연결 가이드](docs/RESILIENCE_ADAPTERS.md), 현재 Hangari 코드에 맞춘 준비 사항은 [Hangari 가이드](docs/HANGARI_CONNECTION.md)에 있습니다. Hangari 원본에는 변경을 적용하지 않았습니다.

### 예: 로컬 Hangari 서버 연결

Hangari를 `local` 프로필로 http://localhost:10000 에 띄운 상태에서 진행합니다. 로컬 프로필은 JWT가 아닌 `identityToken`을 합성 테스트 사용자(`local.*`)로 처리합니다.

1. 테스트 로그인으로 토큰을 받습니다. `Device-Id` 헤더가 필요하며, 토큰은 응답의 `Authorization` 헤더로 옵니다.

   ```bash
   curl -i -X POST http://localhost:10000/api/v1/auth/social-login \
     -H 'Content-Type: application/json' -H 'Device-Id: burstguard-device-1' \
     -d '{"provider":"APPLE","authorizationCode":"test","identityToken":"burstguard-test-1","nonce":"test"}'
   ```

2. 처음 로그인한 사용자는 `ONBOARDING_REQUIRED`입니다. 위 토큰으로 온보딩하면 응답 헤더로 새 토큰이 옵니다. 검색 API는 온보딩된 토큰이 있어야 200을 반환합니다.

   ```bash
   curl -i -X POST http://localhost:10000/api/v1/auth/onboarding \
     -H "Authorization: Bearer <1번 토큰>" -H 'Device-Id: burstguard-device-1' -H 'Content-Type: application/json' \
     -d '{"name":"BurstGuard Tester","gender":"MALE","countryCode":"KOR","languageCode":"KOR","birthDate":"2000-01-01","appEntrySource":"APP_STORE"}'
   ```

3. 실험실에서 대상 주소 `http://localhost:10000`, 장애 모드 **트래픽만 측정**, 비교 끔, 고급 설정의 Bearer 토큰에 2번 토큰을 넣고, [Hangari 가이드](docs/HANGARI_CONNECTION.md)의 콘텐츠 목록·검색 행동을 사용합니다.

2026-10-22 실측(같은 컴퓨터, 사용자 200명·부하 60초): 66,854건 모두 200, 실패·타임아웃 0건, P95 20ms → 173ms → 회복 22ms. Hangari는 Actuator가 없어 DB 풀 고갈 여부는 판정하지 않았습니다.

실제 자원 지표를 읽을 수 있는 권한이 필요하며, 토큰은 실행마다 입력합니다. 토큰·응답 본문·추출한 세션 값은 기록하지 않습니다. 테스트 본문 자체는 계획에 저장되므로 합성 테스트 값으로 작성합니다. 자동 제어 설정은 종료·실패·중단 시 원래 값으로 복원을 시도하며, 실패하면 결과에 표시합니다. 중단된 HTTP 요청이 이미 서버에서 시작됐다면 서버 타임아웃까지 남을 수 있습니다.

## 검증

```bash
LANGSMITH_TRACING=false .venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_resilience.py --users 70 --ai
.venv/bin/python scripts/verify_extended.py
```

신규 회귀 테스트는 대상 경계, 인증 실패 시 부하 중단, 실제 SQLite 풀의 자원 반환, 사용자별 인증, 200 응답의 업무 오류 판정, 중단 시 장애 복원, 변경 전후 비교, 누락 지표의 판정 제한을 검증합니다. 실제 HTTP 실험은 테스트 대역을 쓰는 회귀 테스트와 별도로 기록합니다. 결과는 [실측 검증](docs/RESILIENCE_VERIFICATION.md)에 정리합니다.

## 판정 범위

전체 오류를 자동으로 찾는 도구는 아닙니다. 작성한 행동·부하·장애 조건에서 실제로 발생한 문제를 관측합니다. 가상 사용자는 순차 요청과 대기 시간을 갖는 폐쇄형 부하 모델이며 고정 RPS, 실제 브라우저 인원, 운영 트래픽 전체를 뜻하지 않습니다. 실패·타임아웃도 지연 통계에 포함됩니다. 부하 생성기와 대상 앱이 같은 컴퓨터를 쓰므로 절대 용량 산정보다는 재현·전후 비교에 적합합니다.

기존 CrashCustomer 관련 문서와 과거 평가 파일은 복사본의 `docs/`와 `README_CRASHCUSTOMER.md`에 보존했습니다. **그 수치를 새 부하 엔진이나 실제 Hangari의 성능으로 사용하지 않습니다.** 신규 기술 구조와 판정 기준은 [새 구조 문서](docs/RESILIENCE_ARCHITECTURE.md)를 기준으로 봅니다.
