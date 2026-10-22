# LangSmith 클라우드 수신 검증 · 2026-10-22

## 결과: verified

새 메인 AI 감독으로 합성 앱의 원래 조건과 낮은 부하 대조를 실행한 뒤, 실제 역할별 추적의 메타데이터를 LangSmith에 전송하고 **13/13개를 다시 조회해 일치 확인**했다. SDK 모의 transport 테스트나 로컬 JSON 생성만으로 판정한 결과가 아니다.

- 프로젝트: `burstguard-cloud-verification`
- 소스 실험 ID: `fcdca7305d0d4560bbcb54fbcbc28637`
- 소스 로컬 루트 ID: `01a1617a-ad10-7251-8fca-7667448a9d5a`
- 클라우드 루트 ID: `f51fbf09-5ff8-47ad-ad1f-0e8c791f74b3`
- 확인 시각: **2026-10-22 11:41:08 KST**
- 완료한 추적 13개: 감독 루트 1개와 역할별 자식 12개.
- LLM 추적 **4개**: 계획 2회, 검증 2회.
- 도구 추적 **3개**: 근거 검색 1회, 실행 작업자 2회.
- ID·부모 ID·trace ID·역할 이름·유형·종료 상태·입출력·검증 nonce 일치. 시작/종료 시각은 1ms 이내로 대조.
- 누락 ID **0개**, 불일치 ID **0개**.

[LangSmith에서 해당 추적 열기](https://smith.langchain.com/o/9ff2f04c-2b5a-44fc-9d69-8a126e701d1c/projects/p/b879911d-c08e-4209-8695-023863cea9cf/r/f51fbf09-5ff8-47ad-ad1f-0e8c791f74b3?trace_id=f51fbf09-5ff8-47ad-ad1f-0e8c791f74b3&start_time=2026-10-22T02:39:52.080267)

계정/워크스페이스 권한이 필요한 일반 내부 링크이며 공개 공유 링크를 만들지 않았다.

## 전송한 것과 검증 방식

`burstguard/cloud_trace.py`는 완료한 전용 예제 실행만 허용하고, 기존 추적 딕셔너리를 그대로 전달하지 않는다. 허용된 역할 이름, 실제 시작/종료 시각, 상태, 집계 토큰/호출 수, 허용 MCP 호출명, 부모 관계로 입력과 출력을 다시 만든다. 클라우드 ID와 검증 nonce를 새로 부여해 과거 실행이나 다른 내용이 조회되어 성공으로 판정되는 것을 방지한다.

전송은 **완료한 실행의 메타데이터 내보내기**다. 모든 프롬프트를 실시간 전송하는 전역 tracing 모드와 다르다. 실제 메인 실행의 로컬 추적과 클라우드의 새 ID를 증빙 JSON에서 연결한다.

첨부 원문·사용자 목표 원문·LLM 프롬프트·모델 응답 원문·HTTP 요청/응답 본문·인증값·실제 프로젝트 이름은 추적 데이터에서 제외했다. 연결 키는 기존 로컬 설정 파일에서 읽기만 했고 출력·증빙·소스 묶음에 포함하지 않았다. 일반 실험의 자동 외부 tracing은 계속 비활성이다.

## 증거 파일

- `langsmith-cloud-receipt-20261022.json`: 실제 클라우드 조회 결과, ID, nonce, 개수, URL.
- `agent-live-evidence-20261022.json`: 원래/낮은 부하 실험, MCP·LLM 사용, 실측 비교와 수신 증거 연결.
- `agent-live-trace-20261022.json`: 소스 로컬 SDK 추적과 클라우드 수신 결과.
- `agent-live-report-20261022.md`: 메인 실험 보고서에 수신 확인 반영.
- `cloud-tests-20261022.txt`: 전체 회귀 **212 passed, 1 warning, 30.51초**. 경고는 기존 SDK 메서드 deprecation이다.

7개 추가 검사에서 비합성·미완료·허용되지 않은 역할·틀린 부모 거절, 비허용 본문 제거, 일부 수신을 성공으로 처리하지 않음, nonce/입출력 불일치 거절을 확인했다. 모의 검사는 전송 코드의 동작 확인용이고 실제 수신의 근거는 위 클라우드 조회 JSON이다.

## 재현

서버가 실행 중이고 합성 예제 실험이 완료된 상태에서 작업 폴더 `burstguard`에서 실행한다.

```bash
# 외부 전송 없이 대상과 전송 필드 확인
PYTHONPATH=. .venv/bin/python scripts/verify_resilience_cloud.py <완료한_합성_실험_ID>

# 합성 메타데이터 전송을 명시적으로 선택하고 모든 span을 다시 조회
PYTHONPATH=. .venv/bin/python scripts/verify_resilience_cloud.py <완료한_합성_실험_ID> --allow-synthetic-external-trace
```

연결 설정은 기본적으로 `../crashcustomer/.env`에서 읽으며 `--credentials-file`로 별도 설정 파일을 지정할 수 있다. 연결 파일을 소스 묶음에 넣지 않는다. 공식 LangSmith HTTPS API만 허용한다. 모든 실행을 자동으로 전송하는 UI 버튼이나 공개 API는 추가하지 않았다.
