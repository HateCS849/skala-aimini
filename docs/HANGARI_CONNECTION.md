# Hangari 연결 준비

2026-10-22에 `/Users/changmin/Desktop/hangari-server` 코드를 읽어 작성했습니다. 원본은 수정하거나 실행하지 않았습니다. 이 문서는 실제 Hangari 부하·장애 검증이 완료됐다는 증거가 아닙니다.

## 확인한 구현

- 멀티 모듈 Spring Boot 프로젝트이며 실행 앱은 `hangari-app-server`입니다.
- 로그인: `POST /api/v1/auth/social-login`.
- 로그인 본문 필수 항목: `provider`, `authorizationCode`, `identityToken`, `nonce`.
- 콘텐츠: `GET /api/v1/contents`, 검색: `GET /api/v1/search?keyword=kimchi`.
- 콘텐츠·검색은 인증 사용자 정보를 사용하므로 유효한 테스트 계정 인증이 필요합니다.
- 로그인 응답의 `Authorization` 헤더와 쿠키를 사용합니다.
- `local` 프로필의 `LocalAppleTokenClient`는 실제 Apple 토큰 교환을 생략합니다. 그대로 실행하면 Apple 응답 지연을 재현하지 못합니다.
- `LocalAppleIdTokenValidator`의 테스트용 동작은 기존 local 프로필 코드에 이미 있습니다. BurstGuard가 인증 정책을 변경하지 않습니다.
- 현재 `hangari-app-server/build.gradle`에는 Actuator 스타터 선언이 보이지 않습니다. 지표 API 접근 허용만으로 계측이 생기지는 않습니다.

## 먼저 HTTP 부하부터 연결

1. 별도 테스트 데이터로 DB·Redis와 Hangari를 로컬에 기동합니다.
2. BurstGuard에서 대상 주소를 실제 Hangari 주소로 바꾸고, 비교를 끈 뒤 장애 제어는 **트래픽만 측정**으로 선택합니다.
3. 사용자 행동 JSON에 실제 콘텐츠·검색 요청과 `expect_values: {"status": true}`를 작성합니다. Bearer 토큰은 고급 설정에서 입력합니다.
4. 보호 기능 경로도 `/api/v1/contents` 같은 실제 경로로 변경하고, 필수 JSON 경로를 `data`로 설정합니다.
5. 사전 확인이 401·오류로 중단되면 인증·성공 조건을 먼저 맞춥니다. 실패 응답이 빠르다는 이유로 성능이 좋다고 판정하지 않습니다.

예시 행동:

```json
[
  {"name":"콘텐츠 목록","weight":70,"steps":[{"name":"목록","method":"GET","path":"/api/v1/contents?page=0&size=20","expected_status":[200],"expect_json":"data","expect_values":{"status":true}}]},
  {"name":"검색","weight":30,"steps":[{"name":"검색","method":"GET","path":"/api/v1/search?keyword=kimchi","expected_status":[200],"expect_json":"data","expect_values":{"status":true}}]}
]
```

이는 기존 테스트 토큰으로 조회 부하를 주는 계획입니다. 사용자 70명이 서로 다른 계정이라는 의미는 아닙니다. 사용자별 로그인과 세션은 별도의 행동 steps와 고유한 테스트 계정 데이터로 구성해야 합니다.

## 커넥션 고갈과 Apple 지연까지 검증하려면

Hangari의 별도 테스트 복사본/프로필에서 다음 연결 작업이 필요합니다.

- Actuator 스타터와 테스트용 metrics 노출 설정을 추가해 Hikari active/max/pending을 읽습니다. 여러 풀이면 풀 이름을 지정합니다.
- 외부 HTTP 요청을 실제로 수행하는 테스트용 Apple 연동 구현과 지연을 제어할 대체 서버를 연결합니다. local의 생략 구현을 유지하면 지연 실험이 되지 않습니다.
- DB 풀 10개, 커넥션 획득 대기, 외부 연결·응답 타임아웃을 명시합니다.
- 제어 어댑터를 구현하거나 외부 대체 서버를 수동 설정한 뒤 같은 조건으로 측정합니다. 수동 설정일 때 BurstGuard는 자동 주입했다고 표시하지 않습니다.
- 변경 전후를 서로 다른 로컬 포트로 띄워 비교하거나, 같은 포트에서 버전을 교체하고 계획을 다시 실행합니다.

로그인 지연 → 실제 Hikari 풀 포화·연결 대기 → 콘텐츠·검색 실패가 함께 관측되는지를 확인해야 이 장애의 재현 근거가 됩니다. 코드 원인 확정과 과거 베타 운영 사실 확인은 별개의 작업입니다.
