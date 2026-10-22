"""Evidence sufficiency, separate from whether a measured service has a defect."""


def evidence_packet(record):
    rows = []
    for index, target in enumerate(record.get("targets", [])):
        summary = (record.get("summary") or {}).get("targets", [])
        result = summary[index] if index < len(summary) else {}
        phases = target.get("phases", [])
        rows.append({"label": target["label"], "restore": target.get("fault_restore"),
            "protected_observed": result.get("protected_affected") is not None,
            "phases": [{"name": p["name"], "requests": p.get("total", {}).get("requests", 0),
                "error_rate": p.get("total", {}).get("error_rate"), "p95_ms": p.get("total", {}).get("p95_ms"),
                "pool_samples": sum("pool_active" in m and "pool_max" in m for m in p.get("metrics", [])),
                "budget_hit": p.get("request_budget_hit", False)} for p in phases],
            "checks": [c["status"] for b in target.get("integrity", []) for c in b["checks"]],
            "restart": [r.get("data_status") for r in target.get("restarts", [])],
            "soak": [c["status"] for c in target.get("diagnostics", {}).get("soak", [])],
            "findings": [{"code": f["code"], "title": f["title"], "evidence": f["evidence"]} for f in result.get("findings", [])],
            "limits": result.get("limits", [])})
    return {"execution_status": record.get("status"), "mode": record["config"].get("mode", "load"),
        "expects_comparison": bool(record["config"].get("comparison_url")),
        "metrics_mode": record["config"].get("metrics", {}).get("mode", "none"),
        "expects_protected": bool(record["config"].get("protected_step")),
        "integrity_enabled": record["config"].get("integrity", {}).get("enabled", False),
        "restart_enabled": record["config"].get("restart", {}).get("enabled", False),
        "soak_enabled": record["config"].get("soak", {}).get("enabled", False), "targets": rows}


def validate_evidence(packet):
    issues, supported = [], []
    def issue(code, text):
        issues.append({"id": code, "message": text})
    if packet.get("execution_status") != "completed":
        issue("execution", "실험이 정상적으로 종료되지 않았습니다.")
    targets = packet.get("targets", [])
    if len(targets) != (2 if packet.get("expects_comparison") else 1):
        issue("targets", "설정한 대상 모두의 측정 결과가 필요합니다.")
    for index, target in enumerate(targets):
        prefix = f"t{index}"
        if target.get("restore") in {"failed", "pending"}:
            issue(prefix + ".restore", "장애 설정 복원이 확인되지 않았습니다.")
        if packet.get("mode") != "integrity":
            phases = {p["name"]: p for p in target.get("phases", [])}
            for name in ("baseline", "fault", "recovery"):
                if phases.get(name, {}).get("requests", 0) < (10 if name == "fault" else 3):
                    issue(prefix + ".samples." + name, name + " 요청 표본이 부족합니다.")
            if packet.get("metrics_mode") != "none" and not phases.get("fault", {}).get("pool_samples", 0):
                issue(prefix + ".metrics", "연결하기로 한 DB 풀 지표를 관측하지 못했습니다.")
            if any(p.get("budget_hit") for p in phases.values()):
                issue(prefix + ".budget", "요청 예산에 도달해 전체 관찰 시간을 충족하지 못했습니다.")
            if packet.get("expects_protected") and not target.get("protected_observed"):
                issue(prefix + ".protected", "보호 기능의 기준·장애 표본 또는 정상 기준이 부족합니다.")
        if packet.get("integrity_enabled") and (not target.get("checks") or "unverified" in target["checks"]):
            issue(prefix + ".integrity", "실제 저장 상태로 모든 정합성 검사를 확인해야 합니다.")
        if packet.get("restart_enabled") and (not target.get("restart") or any(s not in {"passed", "failed"} for s in target["restart"])):
            issue(prefix + ".restart", "재시작 전후 데이터 비교 근거가 부족합니다.")
        if packet.get("soak_enabled") and (not target.get("soak") or "unverified" in target["soak"]):
            issue(prefix + ".soak", "자원 추세를 판정할 관찰 시간·표본이 부족합니다.")
        supported.extend({"id": prefix + "." + f["code"], **f} for f in target.get("findings", []))
    return {"verdict": "partial" if issues else "complete", "issues": issues, "supported_findings": supported,
        "meaning": "complete는 측정 근거의 충분성입니다. 서비스에 오류가 없다는 의미가 아닙니다."}


def contrast_experiments(runs):
    primary = next((r for r in runs if r["scenario_id"] == "requested"), None)
    if not primary:
        return []
    rows = []
    for control in runs:
        if control is primary:
            continue
        for original, other in zip((primary.get("summary") or {}).get("targets", []), (control.get("summary") or {}).get("targets", [])):
            sufficient = all(r.get("validation", {}).get("verdict") == "complete" for r in (primary, control))
            changed = original.get("protected_affected") is True and other.get("protected_affected") is False
            rows.append({"target": original["label"], "control": control["scenario_id"],
                "status": "observed_change" if sufficient and changed else "no_protected_change" if sufficient else "unverified",
                "requested_protected_affected": original.get("protected_affected"),
                "control_protected_affected": other.get("protected_affected"),
                "requested_findings": [f["code"] for f in original.get("findings", [])],
                "control_findings": [f["code"] for f in other.get("findings", [])],
                "scope": "같은 로컬 머신의 순차 대조입니다. 내부 코드 원인 확정이나 모든 장애의 격리를 뜻하지 않습니다."})
    return rows
