"""Read observable state before/after isolated, bounded write batches."""
import asyncio
import math
from uuid import uuid4

from .engine import aggregate, lookup, render
from .models import relative_path


async def read_numbers(client, step, variables, paths):
    response = await client.get(relative_path(render(step.path, variables)).lstrip("/"))
    response.raise_for_status()
    if response.status_code not in step.expected_status:
        raise ValueError("상태 조회 응답 조건 불일치")
    body = response.json()
    values = {}
    for name, pointer in paths.items():
        try:
            value = lookup(body, pointer)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                values[name] = value
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    return values


async def run_integrity(runner, record, target, spec):
    config = spec.integrity
    outcomes = []
    target["integrity"] = outcomes
    runner.emit(record, "integrity", f"{target['label']} · 동시 요청 전후의 재고·주문·중복 키를 확인합니다.")
    async with runner.client(target["url"], spec) as client:
        for case in dict.fromkeys(config.cases):
            for turn in range(config.rounds):
                scope = f"{record['id']}-{case}-{turn}"
                variables = {"experiment_id": scope, "vu": "setup", "iteration": 0, "uuid": uuid4().hex}
                result = {"case": case, "round": turn + 1, "scope": scope, "checks": [], "status": "running"}
                outcomes.append(result)
                try:
                    if config.setup:
                        setup = await runner.request_step(client, config.setup, variables)
                        if not setup["ok"]:
                            raise ValueError("실험용 상태 준비 요청 실패")
                    paths = {"stock": config.stock_path, "orders": config.orders_path, "unique_keys": config.unique_keys_path}
                    before = await read_numbers(client, config.snapshot, variables, paths)
                    count = config.stock_requests if case == "stock" else config.duplicate_copies
                    gate = asyncio.Semaphore(spec.users)

                    async def mutate(index):
                        values = {**variables, "vu": index, "request_key": f"{scope}-" + (str(index) if case == "stock" else "same")}
                        async with gate:
                            return await runner.request_step(client, config.mutation, values)

                    responses = await asyncio.gather(*(mutate(i) for i in range(count)))
                    await asyncio.sleep(config.settle_seconds)
                    after = await read_numbers(client, config.snapshot, variables, paths)
                    result.update(before=before, after=after, requests=aggregate(responses, 0))

                    def check(code, title, valid, evidence, observed=True):
                        result["checks"].append({"code": code, "title": title, "status": "unverified" if not observed else "passed" if valid else "failed", "evidence": evidence})

                    known = all(k in before and k in after for k in ("stock", "orders"))
                    if not known or before.get("stock", 0) < config.quantity:
                        check("state_observable", "검증 가능한 초기 재고·주문 상태", False, "상태 숫자가 없거나 초기 재고가 부족해 판정하지 않았습니다.", observed=False)
                    else:
                        created = after["orders"] - before["orders"]
                        consumed = before["stock"] - after["stock"]
                        check("stock_nonnegative", "재고가 음수가 되지 않음", after["stock"] >= 0, f"재고 {before['stock']} → {after['stock']}")
                        check("no_oversell", "초기 재고를 넘는 주문 없음", 0 <= created * config.quantity <= before["stock"], f"초기 재고 {before['stock']}, 새 주문 {created}, 주문당 수량 {config.quantity}")
                        check("stock_balance", "재고 차감과 생성 주문 수 일치", consumed == created * config.quantity, f"차감 {consumed}, 생성 주문의 기대 차감 {created * config.quantity}")
                        if case == "idempotency":
                            keys_known = "unique_keys" in before and "unique_keys" in after
                            new_keys = after.get("unique_keys", 0) - before.get("unique_keys", 0)
                            # At least one durable result is required; rejecting every request is not idempotency.
                            valid = created == 1 and new_keys == 1 and consumed == config.quantity
                            check("idempotency", "동일 요청 키의 효과는 정확히 한 번", valid,
                                  f"동일 키 {count}회 → 새 주문 {created}, 새 고유 키 {new_keys if keys_known else '미확인'}", observed=keys_known)
                    result["status"] = "completed"
                except asyncio.CancelledError:
                    result["status"] = "interrupted"
                    raise
                except Exception as error:
                    result.update(status="unverified", error=type(error).__name__)
                    result["checks"].append({"code": "state_observable", "title": "전후 상태 확인", "status": "unverified", "evidence": "준비·요청·상태 조회를 완료하지 못했습니다."})
                finally:
                    runner.records.save(record)
