"""Evidence-only extended diagnostics. Missing samples never pass a check."""
import statistics


def counter_delta(points, name):
    values = [p[name] for p in points if isinstance(p.get(name), (int, float))]
    if len(values) < 2 or any(b < a for a, b in zip(values, values[1:])):
        return None
    return values[-1] - values[0]


def trend(points, key, seconds, minimum, relative=.2):
    rows = [(p["second"], p[key]) for p in points if isinstance(p.get(key), (int, float))]
    if len(rows) < 8 or rows[-1][0] - rows[0][0] < seconds:
        return {"status": "unverified", "metric": key, "reason": "관찰 시간·표본 부족"}
    rows = rows[len(rows) // 5:]  # Exclude initial warm-up.
    width = max(1, len(rows) // 3)
    windows = [statistics.mean(v for _, v in rows[i * width:(i + 1) * width]) for i in range(2)]
    windows.append(statistics.mean(v for _, v in rows[2 * width:]))
    growth = windows[-1] - windows[0]
    suspect = growth >= minimum and growth >= abs(windows[0]) * relative and windows[0] < windows[1] < windows[2]
    return {"status": "growth" if suspect else "no_growth_observed", "metric": key, "window_means": [round(v, 2) for v in windows], "growth": round(growth, 2), "samples": len(rows), "scope": "지속 증가 징후이며 누수 원인 확정은 아닙니다."}


def extended(target, spec):
    findings, limits = [], []

    def add(code, title, evidence, severity="warning"):
        findings.append({"code": code, "title": title, "evidence": evidence, "severity": severity})

    for batch in target.get("integrity", []):
        for check in batch["checks"]:
            if check["status"] == "failed":
                add("integrity_" + check["code"], check["title"] + " · 위반", check["evidence"], "critical")
            elif check["status"] == "unverified":
                limits.append("정합성 미확인: " + check["evidence"])
    phase = next((p for p in target.get("phases", []) if p["name"] == "fault"), {})
    points = phase.get("metrics", [])
    attempts, logins = counter_delta(points, "provider_attempts"), counter_delta(points, "logical_auth_requests")
    diagnostic = {}
    if attempts is not None and logins is not None and logins >= 3:
        ratio = attempts / logins
        diagnostic["retry"] = {"attempts": attempts, "logical_requests": logins, "amplification": round(ratio, 2)}
        if ratio > 3:
            add("retry_amplification", "실패 시 외부 호출이 3배 넘게 증폭", f"논리 로그인 {logins:g}건에 외부 호출 {attempts:g}회 · {ratio:.2f}배", "critical")
    elif spec.external_status != 200 or spec.faults.external_behavior != "normal":
        limits.append("외부 시도·논리 요청 계측이 부족해 재시도 증폭은 미확인입니다.")
    for metric, code, title in (("cache_errors", "cache_dependency_errors", "캐시 연결 실패 관측"), ("db_lock_errors", "db_lock_contention", "실제 DB 쓰기 잠금 충돌 관측")):
        delta = counter_delta(points, metric)
        if delta is not None and delta > 0:
            diagnostic[metric] = delta
            add(code, title, f"부하 단계 누적 카운터 증가 {delta:g}건. 사용자 응답 영향은 행동별 결과로 확인합니다.")
    for event in target.get("restarts", []):
        if event.get("data_status") == "failed":
            add("restart_data_changed", "재시작 전후 기준 데이터 불일치", "비교한 숫자 필드: " + ", ".join(event.get("changed_paths", [])), "critical")
        elif event.get("data_status") != "passed":
            limits.append("재시작 전후 데이터 보존은 미확인입니다.")
    if spec.soak.enabled:
        checks = [("process_rss_bytes", spec.soak.rss_growth_mb * 1024 * 1024, "프로세스 메모리"),
                  ("heap_used_bytes", spec.soak.rss_growth_mb * 1024 * 1024, "애플리케이션 힙 메모리"),
                  ("open_fds", spec.soak.fd_growth, "열린 파일·소켓"), ("process_threads", spec.soak.thread_growth, "프로세스 스레드")]
        diagnostic["soak"] = []
        for key, minimum, title in checks:
            outcome = trend(points, key, spec.soak.min_observation_seconds, minimum)
            outcome["title"] = title
            diagnostic["soak"].append(outcome)
            if outcome["status"] == "growth":
                add("growth_" + key, title + " 지속 증가 징후", f"안정화 이후 구간 평균 {outcome['window_means']} · 증가 {outcome['growth']}. 누수 원인 확정은 아닙니다.")
            elif outcome["status"] == "unverified":
                limits.append(title + " 장시간 추세 미확인: " + outcome["reason"])
    target["diagnostics"] = diagnostic
    # A code may fail in multiple rounds; preserve each round's evidence as separate findings.
    return findings, list(dict.fromkeys(limits))
