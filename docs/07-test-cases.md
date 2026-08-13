# 07. 테스트케이스·기대 결과표

> 버전: v1.0 · 기준일: 2026-08-10 · 상태: **MVP 검증 최종본**

## 1. 테스트 원칙

- 솔버가 만든 결과는 별도 `plan_validator`로 다시 검산한다.
- 상태·사유·배정·대안은 함께 검증한다.
- 기본안과 대안은 서로 다른 시나리오 ID를 가져야 한다.

## 2. 필수 케이스

| ID | 입력 장면 | 기대 결과 |
| --- | --- | --- |
| TC-01 | 조건 충족 주문과 빈 슬롯 | 정확히 한 슬롯 배정, `편성 가능` |
| TC-02 | 총중량 또는 납기 누락 | `REVIEW_REQUIRED`, `MISSING_REQUIRED_FIELD`, 솔버 입력 제외 |
| TC-03 | `ready_at`이 반입 마감 이후 | 기본 `INELIGIBLE`; 다음 운행이 허용되면 `AVAILABLE` |
| TC-04 | 운행 도착·처리 완료가 납기 이후 | `INELIGIBLE + NONE`, `DUE_TIME_EXCEEDED` |
| TC-05 | 슬롯·화차 중량 한도 초과 | 배정 불가; 대안이 중량 한도를 바꾸지 않음 |
| TC-06 | 슬롯 또는 터널 높이·폭 초과 | 후보 제외, `DIMENSION_*` 사유 |
| TC-07 | 터미널 취급 태그 불일치 | 기본 불가; 승인된 대체 터미널만 대안 후보 |
| TC-08 | 적합 주문 수가 슬롯 수보다 많음 | 선택 주문만 배정, 나머지 `ELIGIBLE + UNASSIGNED + CAPACITY_CONFLICT` |
| TC-09 | 승인된 대체 터미널 선택 시 편성 가능 | 기본안 보존, 파생 대안에서만 배정 가능 |
| TC-10 | 금지된 완화 제약 변경을 직접 요청 | `409 POLICY_VIOLATION`; 기존 대안 상태는 바꾸지 않음 |
| TC-11 | 동등한 목적값의 복수 배정 | 정책의 안정적 동률 규칙으로 같은 결과 선택 |
| TC-12 | 적합 후보가 전혀 없음, 전역 잠금 없음 | 배정 0건의 유효 실행; 주문만 `INELIGIBLE` |
| TC-13 | 동일 슬롯에 두 주문을 강제 잠금 | **Solver 단위 테스트용 내부 고정배정 입력**으로 `MODEL_INFEASIBLE`, 채택 차단 |
| TC-14 | 기본 집합 밖의 다음 운행이 기본안에 사용됨 | 독립 검증 실패 |
| TC-15 | 대안이 기존 배정 주문을 이동시킴 | 모든 영향 주문과 필수 `assignment_deltas`의 before/after 기록 |
| TC-16 | 동일 입력·단일 worker·최적 종료 반복 | 정규화 결과 해시 동일 |
| TC-17 | 시간 제한으로 `FEASIBLE + PASS` 반환 | 결과 조회·보류 가능, `ACCEPTED` 요청은 `409 RUN_NOT_ACCEPTABLE` |
| TC-18 | `GET /runs/{run_id}/export` | 시나리오·입력 스냅샷·정책·실행 설정·hash·검증·결정·trace가 한 ExportBundle에 존재 |

## 3. 테스트 계층

| 계층 | 대상 | 자동화 예 |
| --- | --- | --- |
| Contract | JSON 형식·필수값·단위 | Pydantic/JSON Schema 테스트 |
| Rule | 시간·중량·규격·터미널 함수 | 경계값·반례 테스트 |
| Solver | 배정·목적함수·동률 | 정본 fixture 결과 비교 |
| Validator | 잘못된 배정 거부 | 고의로 중복·초과 배정한 반례 |
| Alternative | 허용/금지 변경·영향 주문 | HTTP 201/200/409와 `assignment_deltas` 비교 |
| API/UI | 상태·사유·화면 행동 | 엔드포인트·핵심 흐름 테스트 |

## 4. 검증 통과 조건

- 모든 배정안에서 주문·슬롯 중복이 없다.
- 하드 제약 위반이 없다.
- `ORDER_OUTCOME`의 상태 축이 실제 후보와 배정 결과에 일치한다.
- 성공 대안의 `change_set`이 정책 허용 목록 밖을 변경하지 않고, `assignment_deltas`가 영향 주문·전후 배정과 일치한다.
- `validator_status = FAIL`이면 UI에서 채택 행동을 제공하지 않는다.

## 5. 정본 결과 파일 형식 (`expected-results.json`)

```json
{
  "ORD-004": {
    "input_state": "VALID",
    "eligibility_state": "ELIGIBLE",
    "assignment_state": "UNASSIGNED",
    "alternative_state": "NOT_SEARCHED",
    "primary_reason_code": "CAPACITY_CONFLICT"
  },
  "ORD-006": {
    "input_state": "REVIEW_REQUIRED",
    "eligibility_state": "NOT_EVALUATED",
    "assignment_state": "NOT_APPLICABLE",
    "alternative_state": "NOT_SEARCHED",
    "primary_reason_code": "MISSING_REQUIRED_FIELD"
  }
}
```

## 6. 정본 fixture 추적표

| 테스트 | fixture 주문/값 | API 증거 | 기대값 |
| --- | --- | --- | --- |
| TC-02 | `ORD-006.gross_weight_kg = null` | `/validate` | `REVIEW_REQUIRED`, `MISSING_REQUIRED_FIELD` |
| TC-03 | `ORD-005.ready_at = 11:00`, 기본 마감 10:30 | `/scenarios/{scenario_id}/runs`, `/runs/{run_id}/alternatives` | 기본 `READY_AFTER_CUTOFF`, 다음 운행 배정 |
| TC-06 | `ORD-007.height = 4300`, 경로 한도 4000 | `/scenarios/{scenario_id}/validate` | `TUNNEL_HEIGHT_EXCEEDED` |
| TC-07 | `ORD-008`의 기본 도착 `TRM-B` | `/runs/{run_id}/alternatives` | `TRM-C`·`SVC-AC-01` 파생 배정 |
| TC-08 | 적합 주문 4건, 기본 슬롯 3개 | `/scenarios/{scenario_id}/runs` | `ORD-004`는 `CAPACITY_CONFLICT` |
| TC-10 | `negative_api_cases.TC-10` | `/runs/{run_id}/alternatives` | `409 POLICY_VIOLATION`; 기준안 상태 불변 |
| TC-15 | 대안 성공 결과 | `/runs/{run_id}/alternatives` | `impacted_order_ids`와 `assignment_deltas` 일치 |
| TC-16 | 같은 `scenario.json`, seed 7, worker 1 | `/scenarios/{scenario_id}/runs` 반복 | 같은 정규화 결과 hash와 실행 설정 |
| TC-17 | `solver_status = FEASIBLE`, `validator_status = PASS` | `/runs/{run_id}/decisions` | `HELD` 허용, `ACCEPTED`는 `409 RUN_NOT_ACCEPTABLE` |
| TC-18 | PASS 실행과 결정 기록 | `/runs/{run_id}/export` | Scenario, Input snapshot, Policy, Run(reproducibility 포함), Validation, Decisions, Trace 반환 |

## 7. 자동화 완료 기준

- fixture JSON 파싱·참조 무결성 테스트가 통과한다.
- `expected-results.json`의 모든 기본 배정·주문 상태와 실행 결과가 일치한다.
- API 응답이 `docs/openapi.yaml`의 Schema 검증을 통과한다.
- Plan Validator가 위반 배정 반례를 하나 이상 거부한다.

## 8. 정규화·해시와 FEASIBLE 비교

정규화는 주문 ID 오름차순으로 `order_outcomes`를 정렬하고, 배정은 `order_id`, `service_id`, `wagon_id`, `slot_id` 순으로 정렬한다. `created_at`, `run_id`, 실행시간처럼 매번 달라지는 값은 제외한다. 남은 JSON을 UTF-8, 키 사전순, 공백 없는 canonical serialization으로 직렬화해 SHA-256을 구한다.

모든 사전순 단계가 `OPTIMAL`인 단일-worker 실행만 해시 동일성을 승인 기준으로 쓴다. `FEASIBLE`은 해시 비교 대신 하드 제약 통과, 목적값, best bound, 종료 사유, validator PASS를 비교한다.
