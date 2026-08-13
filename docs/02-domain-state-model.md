# 02. 도메인·상태 정의서

> 버전: v1.0 · 기준일: 2026-08-10 · 상태: **구현 계약 최종본**

## 1. 공통 규칙

- 모든 엔터티 ID는 불변 문자열이다.
- 시간은 시간대를 포함한 ISO 8601, 데모 시간대는 `Asia/Seoul`로 기록한다.
- 중량은 정수 `kg`, 길이·폭·높이는 정수 `mm`를 사용한다.
- 모든 운영값에는 `PUBLIC_CONFIRMED`, `INSTITUTION_CONFIRMATION_REQUIRED`, `DEMO_ASSUMPTION`, `DERIVED_RESULT` 중 하나의 출처 수준을 붙인다.

## 2. 핵심 엔터티

| 엔터티 | 의미 | 필수 식별·연결 값 |
| --- | --- | --- |
| `Scenario` | 한 번의 계산에 쓰는 불변 입력 스냅샷 | `scenario_id`, `baseline_service_ids`, `policy_version` |
| `Order` | 독립적으로 슬롯 하나에 배정 가능한 운송 단위 | `order_id`, 터미널 집합, 시간, 규격, 중량 |
| `Service` | 특정 출발·도착 시각의 운행 후보 | `service_id`, 터미널, 시각, 마감 |
| `Wagon` | 운행에 연결된 화차 | `wagon_id`, `service_id`, 총중량 한도 |
| `Slot` | 주문을 배정하는 최소 자원 단위 | `slot_id`, `wagon_id`, 위치, 규격·중량 한도 |
| `Terminal` | 반입·상하역 조건을 가진 합류/도착 거점 | `terminal_id`, 운영·취급 규칙 |
| `Policy` | 제약·목적함수·대안 허용 범위 | `policy_id`, `policy_version` |
| `Run` | 솔버 실행과 검증 결과 | `run_id`, `scenario_id`, `solver_status` |
| `Decision` | 운영자의 채택·보류·반려 기록 | `decision_id`, `run_id`, 상태, 사유 |

## 3. 주문 상태 축

주문 결과는 하나의 상태로 저장하지 않는다. 아래 네 축을 개별 보존한다.

| 필드 | 값 | 질문 |
| --- | --- | --- |
| `input_state` | `VALID`, `REVIEW_REQUIRED` | 계산에 쓸 입력이 충분한가? |
| `eligibility_state` | `ELIGIBLE`, `INELIGIBLE`, `NOT_EVALUATED` | 기본 시나리오에 적합 후보가 있는가? |
| `assignment_state` | `ASSIGNED`, `UNASSIGNED`, `NOT_APPLICABLE` | 기본안에 실제 배정됐는가? |
| `alternative_state` | `AVAILABLE`, `NONE`, `NOT_SEARCHED` | 허용된 변경으로 파생 대안이 있는가? |

`decision_state`는 주문 상태가 아니라 실행 계획에 연결된 별도 `Decision` 기록의 값이며, `ACCEPTED`, `HELD`, `REJECTED`만 사용한다.

## 4. 화면 표시 규칙

1. `input_state = REVIEW_REQUIRED` → **확인 필요**
2. `ELIGIBLE + ASSIGNED` → **편성 가능**
3. `ELIGIBLE + UNASSIGNED` → **편성 가능·미배정**
4. `alternative_state = AVAILABLE` → 기존 주 상태를 바꾸지 않고 **조건부 대안 있음** 배지 추가
5. `INELIGIBLE + NOT_SEARCHED` → **기본안 불가·대안 미검토**
6. `INELIGIBLE + NONE` → **불가**
7. `INELIGIBLE + AVAILABLE` → **기본안 불가**

`불가`는 규칙 6 전용이며 `alternative_state = NONE`에만 쓴다. 승인된 변경을 탐색해
대안을 찾은 주문에 `불가`를 붙이면 규칙 4의 **조건부 대안 있음** 배지와 한 줄에서
정면으로 모순되고, 01 §2의 네 번째 판단(물리적으로 불가한지, 승인된 변경으로 재검토
가능한지)을 화면에서 지운다. 규칙 7의 주 상태는 **기본안이 이 주문을 실을 수 없다**는
사실까지만 말하고, 대안이 있다는 사실은 규칙 4의 배지가 전한다. 규칙 5의 `대안 미검토`도
탐색이 끝난 뒤에는 쓸 수 없다. 세 라벨은 `alternative_state`의 세 값에 하나씩 대응한다.

`OPTIMAL`, `FEASIBLE`, `INFEASIBLE`은 주문 상태가 아닌 **실행 단위 상태**다. 특히 배정이 0건인 유효 실행을 `MODEL_INFEASIBLE`로 표시하지 않는다.

## 5. 하드 제약

- 주문당 배정 수는 최대 1개
- 슬롯당 배정 수는 최대 1개
- 주문의 출발·도착 터미널과 운행 경로가 호환됨
- `ready_at`과 터미널 처리시간을 고려해 반입 마감 전 준비 가능
- 운행 도착과 도착 터미널 처리 후 `due_at` 이내 완료
- 주문 규격·중량·호환 태그가 슬롯과 화차 한도 이내
- 노선 구간의 터널 높이·폭 및 기타 경로 여유 제약 이내
- 운행·화차·슬롯이 가용 상태

## 6. 사유 코드 규칙

| 계열 | 예시 | 의미 |
| --- | --- | --- |
| `INPUT_*` | `MISSING_REQUIRED_FIELD` | 입력 누락·단위·참조·시간 모순 |
| `TIME_*` | `READY_AFTER_CUTOFF`, `DUE_TIME_EXCEEDED` | 준비·반입·납기 문제 |
| `DIMENSION_*` / `WEIGHT_*` | `TUNNEL_HEIGHT_EXCEEDED`, `SLOT_WEIGHT_EXCEEDED` | 규격·중량 한도 초과 |
| `TERMINAL_*` | `TERMINAL_NOT_COMPATIBLE` | 터미널 취급·운영 불가 |
| `RESOURCE_*` | `SLOT_UNAVAILABLE` | 운행·화차·슬롯 가용성 문제 |
| `CAPACITY_*` | `CAPACITY_CONFLICT` | 적합하지만 정책·용량 경합으로 미배정 |
| `ALTERNATIVE_*` | `ALTERNATIVE_AVAILABLE` | 대안 탐색 결과 |

### 6.1 `primary_reason_code` 결정 규칙

한 주문이 여러 하드 제약을 동시에 위반할 수 있다(예: `ORD-008`은 터미널 취급 불가와 높이 초과를 함께 위반한다). `primary_reason_code`는 다음 계열 우선순위에서 **가장 앞선 계열의 위반 코드 하나**로 고정한다. 이 순서를 지켜야 엔진과 독립 검증기가 같은 primary 코드를 내고 결과 해시가 재현된다.

```
INPUT_ > TIME_ > TERMINAL_ > DIMENSION_/WEIGHT_ > RESOURCE_ > CAPACITY_ > ALTERNATIVE_
```

- 같은 계열 안에서 위반이 여럿이면 사유 코드 문자열 사전순으로 정한다.
- 위반하지 않은 나머지 사유는 `order_outcome.evidence`에 함께 보존하되 primary로 승격하지 않는다.
- 배정에 성공한 주문은 위반이 없으므로 `primary_reason_code = "ASSIGNED"`를 쓴다. 이는 위 위반 계열이 아닌 상태 표시용 sentinel 코드다.

## 7. 대안과 기본안의 경계

- 기본안은 `baseline_service_ids`와 원래 주문값만 사용한다.
- 다음 운행과 대체 터미널 변경만 `parent_scenario_id`와 `change_set`을 가진 파생 시나리오에서 적용한다.
- 성공한 대안은 `impacted_order_ids`와 필수 `assignment_deltas`로 전후 배정을 보여 준다. `ADDED`, `MOVED`, `UNASSIGNED` 각각에 `before_assignment`·`after_assignment`을 보존한다.
- 화차 중량, 슬롯 규격, 터널 여유, 확정 납기는 대안에서 바꿀 수 없다.

## 8. API 필드 정본

| 객체 | 필수 필드 | 검증 규칙 |
| --- | --- | --- |
| `Order` | `order_id`, 터미널 집합, `ready_at`, `due_at`, 중량, 치수, 태그, 우선순위 | 중량은 양의 정수, `ready_at < due_at`, 태그는 정책 vocabulary에 존재 |
| `Service` | ID, 출발·도착 터미널, 출발·도착·마감 시각, 경로 제약 ID | `planning_cutoff_at < departure_at < arrival_at` |
| `Slot` | ID, 화차 ID, 위치, 치수·중량 한도, 태그, 가용 여부 | 한도는 양수, 위치는 같은 화차 안에서 유일 |
| `Terminal` | ID, 운영 시간, 최소 처리시간, 태그 | 주문·서비스와 참조 무결성 유지 |
| `Policy` | 버전, 우선순위 점수, 목적함수, 허용·금지 변경 | `objective_order`와 enum 값은 고정 |

## 9. 상태 계산 순서

1. 필수 필드·참조·시간을 검증한다. 실패한 주문은 `REVIEW_REQUIRED`이고 후보를 만들지 않는다.
2. 정상 주문마다 기본 운행 집합 안에서 후보 슬롯을 만든다.
3. 후보가 0개면 `INELIGIBLE + NOT_APPLICABLE`이다. 이때 대안 탐색 전 값은 `NOT_SEARCHED`다.
4. 후보가 있으면 솔버가 기본안 배정을 선택한다. 선택되지 않은 후보는 `ELIGIBLE + UNASSIGNED`다.
5. 대안 실행 뒤에만 `alternative_state`를 `AVAILABLE` 또는 `NONE`으로 갱신한다. 기본 상태는 바꾸지 않는다.

정본 fixture에서 `ORD-005`, `ORD-008`은 대안 실행 후 기본 결과의 보조 상태가 `AVAILABLE`이 되고, `ORD-007`, `ORD-009`는 `NONE`이 된다. 이 전후 값은 `expected-results.json.alternatives`에 고정한다.

`Scenario.state`는 `VALIDATION_REQUIRED`, `READY_TO_SOLVE`, `SOLVED`만 사용한다. 검증 과정에서 `REVIEW_REQUIRED` 주문이 있어도 해당 주문만 솔버에서 제외하고, 나머지 유효 주문을 계산할 수 있으면 시나리오는 `READY_TO_SOLVE`다.

## 10. 제어 vocabulary

- `priority_class`: `P1`, `P2`, `P3`
- `compatibility_tags`: `TRAILER_STANDARD`, `TRAILER_TALL`
- 허용 `change_set.type`: `ADD_ORDER_APPROVED_SERVICE`, `CHANGE_TO_APPROVED_TERMINAL`
- 금지 변경: `CHANGE_WEIGHT_LIMIT`, `CHANGE_DIMENSION_LIMIT`, `CHANGE_ROUTE_CLEARANCE`, `CHANGE_DUE_AT`
