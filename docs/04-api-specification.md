# 04. API 명세서

> 버전: v1.0 · 기준일: 2026-08-10 · 상태: **프론트·백엔드 계약 최종본**

## 1. 공통 규칙

- Base URL: `/v1`
- 요청과 응답: `application/json`
- 시간: ISO 8601 + 시간대
- 모든 실패 응답은 `code`, `message`, `details`, `trace_id`를 포함한다.
- 결과는 검증기 통과 전 `ACCEPTED`로 기록할 수 없다.

## 2. 엔드포인트

| 메서드·경로 | 기능 | 주요 응답 |
| --- | --- | --- |
| `POST /scenarios` | 입력 스냅샷 생성 | `scenario_id`, `state` |
| `POST /scenarios/{scenario_id}/validate` | 입력 검증·후보 판정 | 주문별 검증·후보 결과 |
| `POST /scenarios/{scenario_id}/runs` | 기본 편성 실행 | `run_id`, 실행 상태 |
| `GET /runs/{run_id}` | 편성 결과 조회 | 배정, 주문 결과, 검증 상태 |
| `POST /runs/{run_id}/alternatives` | 허용된 대안 생성 | 파생 `scenario_id`, 비교 결과 |
| `POST /runs/{run_id}/decisions` | 채택·보류·반려 기록 | `decision_id`, 결정 상태 |
| `GET /runs/{run_id}/export` | 데모·검증 보고용 JSON 조회 | 입력·결과·검증·결정 묶음 |

## 3. 시나리오 생성

### `POST /v1/scenarios`

실행 가능한 정본 요청은 아래 조합으로 만든다. `input_snapshot`에 축약 예시를 넣지 않는다.

```text
scenario_name       = canonical-v1-baseline
as_of                = 2026-08-17T08:00:00+09:00
baseline_service_ids = [SVC-AM-01]
policy_version       = 1.0.0
assumption_ids       = [ASM-001, ASM-002]
input_snapshot       = fixtures/canonical-v1/scenario.json 전체 JSON 객체
```

`scenario.json`은 `ScenarioInputSnapshot`의 모든 배열 최소 건수와 참조를 만족하는 유일한 정본 입력이다.

```json
{
  "scenario_id": "SCN-001",
  "state": "VALIDATION_REQUIRED",
  "created_at": "2026-08-10T15:00:00+09:00"
}
```

## 4. 입력 검증과 후보 조회

### `POST /v1/scenarios/{scenario_id}/validate`

응답에는 주문별 입력 상태와 기본 운행의 적합 후보를 포함한다.

```json
{
  "scenario_id": "SCN-001",
  "validation_status": "COMPLETED",
  "orders": [
    {
      "order_id": "ORD-006",
      "input_state": "REVIEW_REQUIRED",
      "reason_codes": ["MISSING_REQUIRED_FIELD"],
      "missing_fields": ["gross_weight_kg"],
      "eligible_slot_ids": []
    }
  ]
}
```

## 5. 편성 실행

### `POST /v1/scenarios/{scenario_id}/runs`

```json
{
  "solver_parameters": {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10}
}
```

```json
{
  "run_id": "RUN-001",
  "scenario_id": "SCN-001",
  "solver_status": "OPTIMAL",
  "run_state": "SOLVED_OPTIMAL",
  "is_optimal": true,
  "validator_status": "PASS",
  "reproducibility": {
    "solver_parameters": {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10},
    "input_snapshot_sha256": "273b4e4683ea40702f05827959a5a9116986c4da4dda711ab2ef47e51f881372",
    "policy_sha256": "081d8735988470546792eb814966aea73fdf745f555ab810331114d41dc4157f",
    "result_sha256": "ef83e8880b1f69249182c5982611a923ff1251e75ca09961bd49acdba7bc7a94"
  },
  "assignments": [],
  "order_outcomes": []
}
```

## 6. 결과 조회

### `GET /v1/runs/{run_id}`

```json
{
  "run_id": "RUN-001",
  "scenario_id": "SCN-001",
  "solver_status": "OPTIMAL",
  "run_state": "SOLVED_OPTIMAL",
  "is_optimal": true,
  "validator_status": "PASS",
  "assignments": [
    {"order_id": "ORD-001", "service_id": "SVC-101", "wagon_id": "WGN-01", "slot_id": "SLT-01"}
  ],
  "order_outcomes": [
    {
      "order_id": "ORD-004",
      "input_state": "VALID",
      "eligibility_state": "ELIGIBLE",
      "assignment_state": "UNASSIGNED",
      "alternative_state": "NOT_SEARCHED",
      "display_label": "편성 가능·미배정",
      "primary_reason_code": "CAPACITY_CONFLICT",
      "next_actions": ["REVIEW_NEXT_SERVICE"]
    }
  ]
}
```

## 7. 조건부 대안

### `POST /v1/runs/{run_id}/alternatives`

```json
{
  "order_id": "ORD-005",
  "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]
}
```

서버는 정책의 허용 범위만 적용한다. 예를 들어 `ADD_ORDER_APPROVED_SERVICE`는 `SVC-NEXT-01`을 추가하는 파생 시나리오를 만들 수 있지만, 높이·중량 한도를 변경하는 요청은 거부한다.

```json
{
  "parent_run_id": "RUN-001",
  "alternative_scenario_id": "SCN-ALT-001",
  "alternative_run_id": "RUN-ALT-001",
  "change_set": [{"type": "ADD_ORDER_APPROVED_SERVICE", "service_id": "SVC-NEXT-01"}],
  "impacted_order_ids": ["ORD-005"],
  "baseline_order_update": {"order_id": "ORD-005", "input_state": "VALID", "eligibility_state": "INELIGIBLE", "assignment_state": "NOT_APPLICABLE", "alternative_state": "AVAILABLE", "primary_reason_code": "READY_AFTER_CUTOFF"},
  "alternative_run_order_outcome": {"order_id": "ORD-005", "input_state": "VALID", "eligibility_state": "ELIGIBLE", "assignment_state": "ASSIGNED", "alternative_state": "AVAILABLE", "primary_reason_code": "ALTERNATIVE_AVAILABLE"},
  "assignment_deltas": [{"order_id": "ORD-005", "change_type": "ADDED", "before_assignment": null, "after_assignment": {"order_id": "ORD-005", "service_id": "SVC-NEXT-01", "wagon_id": "WGN-NEXT-01", "slot_id": "SLT-NEXT-01"}}],
  "validator_status": "PASS"
}
```

허용된 변경을 모두 탐색했지만 대안이 없으면 서버는 `200 NO_FEASIBLE_ALTERNATIVE`와 기준 주문의 `alternative_state = NONE`을 반환한다. 금지된 변경을 직접 요청하면 `409 POLICY_VIOLATION`이다. UI는 허용 변경만 노출하지만 이 409 계약은 API 테스트로 유지한다.

## 8. 결정 기록

### `POST /v1/runs/{run_id}/decisions`

```json
{
  "decision_state": "HELD",
  "actor_role": "SCHEDULING_OPERATOR",
  "reason": "ORD-005 대안의 실제 반입 가능 여부를 확인한다.",
  "selected_plan": "BASELINE"
}
```

`decision_state = ACCEPTED`는 `solver_status = OPTIMAL` **그리고** `validator_status = PASS`일 때만 허용한다. `FEASIBLE + PASS`는 실행 가능한 안으로 조회·비교할 수 있지만 `HELD` 또는 `REJECTED`만 기록할 수 있다. 이를 어기면 `409 RUN_NOT_ACCEPTABLE`이다.

## 9. 오류 코드

| HTTP | 코드 | 의미 |
| --- | --- | --- |
| 400 | `INVALID_INPUT` | 스키마·단위·시간 형식 오류 |
| 404 | `SCENARIO_NOT_FOUND` | 존재하지 않는 시나리오/실행 |
| 409 | `POLICY_VIOLATION` | 금지된 대안 변경 또는 상태 전이 |
| 409 | `RUN_NOT_ACCEPTABLE` | `OPTIMAL + PASS` 외 실행의 `ACCEPTED` 요청 |
| 422 | `VALIDATION_REQUIRED` | 검증 전 실행·결정 요청 |
| 503 | `SOLVER_UNAVAILABLE` | 실행 엔진 일시 실패 |

## 10. OpenAPI 정본과 상태 전이

전체 JSON Schema와 HTTP 계약의 정본은 `docs/openapi.yaml`이다. 이 문서는 읽기 쉬운 보조 설명이며, 구현은 OpenAPI의 required·enum·응답 코드를 따라야 한다.

| 현재 상태 | 요청 | 성공 후 상태 | 거부 조건 |
| --- | --- | --- | --- |
| `VALIDATION_REQUIRED` | `POST /scenarios/{scenario_id}/validate` | `READY_TO_SOLVE` | 존재하지 않는 참조 |
| `READY_TO_SOLVE` | `POST /scenarios/{scenario_id}/runs` | `SOLVED_*` | 검증 미실행·정책 없음 |
| `SOLVED_*` | `POST /runs/{run_id}/alternatives` | 새 `SCN-ALT-*`·`RUN-ALT-*` | 허용 목록 밖 변경 |
| `SOLVED_OPTIMAL` + `PASS` | `POST /runs/{run_id}/decisions` | `ACCEPTED`, `HELD`, `REJECTED` 저장 | 없음 |
| `SOLVED_FEASIBLE` + `PASS` | `POST /runs/{run_id}/decisions` | `HELD`, `REJECTED` 저장 | `ACCEPTED` 요청은 `409 RUN_NOT_ACCEPTABLE` |

`POST /scenarios/{scenario_id}/runs`의 `num_search_workers`는 항상 `1`이며, 이 값이 다르면 `400 INVALID_INPUT`으로 거부한다. 이렇게 해야 fixture의 동률 처리와 결과 해시를 재현할 수 있다.

## 11. 실제 입력·export 계약

- `input_snapshot`은 자유형 object가 아니라 `ScenarioInputSnapshot`이다. assumptions, shippers, terminals, routes, services, wagons, slots, orders, policy가 모두 typed Schema로 검증된다.
- `gross_weight_kg = null`은 정본의 `ORD-006`처럼 `REVIEW_REQUIRED`를 재현하기 위한 유일한 허용 null이다. 누락된 키는 `400 INVALID_INPUT`이다.
- `GET /v1/runs/{run_id}/export`는 시나리오, **원본 입력 스냅샷·정책**, 실행 결과, 검증 결과, 결정 기록, 추적 이벤트와 `reproducibility`(솔버 설정·입력/정책/결과 hash)를 `ExportBundle`으로 반환한다.
