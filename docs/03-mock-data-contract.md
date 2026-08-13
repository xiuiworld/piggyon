# 03. 목업 데이터 계약·정본 시나리오

> 버전: v1.0 · 기준일: 2026-08-10 · 상태: **정본 fixture 계약**

## 1. 원칙

현재 실제 피기백 운영·화물 데이터가 없으므로, 이 문서의 데이터는 모두 **`DEMO_ASSUMPTION` 목업**이다. 실제 운행 가능성, 운영 성과, 예측 확률을 주장하는 근거로 사용하지 않는다.

## 2. 정본 시나리오 규모

| 항목 | 권장 수량 | 목적 |
| --- | ---: | --- |
| 가상 화주 | 5개 | 지도와 주문 출처 구분 |
| 주문 | 9개 | 정상·예외·대안 상태 재현 |
| 터미널 | 3개 | 합류·대체 터미널 비교 |
| 운행 | 3개 | 기본 2건, 대안 전용 다음 운행 1건 |
| 화차 | 3량 | 화차별 배치 시각화 |
| 기본 가용 슬롯 | 6개 | 적합 주문 간 용량 경합 발생 |

## 3. 파일 구성

```text
fixtures/canonical-v1/
  scenario.json          # 모든 입력·정책·가정의 불변 스냅샷
  expected-results.json  # 기본안과 대안의 기대 결과
```

## 4. 파일별 최소 필드

| 파일 | 최소 필드 |
| --- | --- |
| `scenario.json.assumptions` | `assumption_id`, `description`, `source_type`, `impact_scope` |
| `scenario.json.shippers` | `shipper_id`, `display_name`, `pickup_geo`, `delivery_geo` |
| `scenario.json.terminals` | `terminal_id`, 운영시간, 처리시간, 취급 태그 |
| `scenario.json.services` | 출발·도착·마감 시각, 가용 상태, 경로 제약 |
| `scenario.json.wagons` / `slots` | 화차·슬롯 한도, 가용성, 배치 표시 위치 |
| `scenario.json.orders` | 주문·화주 ID, 터미널, 시간, 중량, 치수, 태그, 우선순위, 승인된 대안 |
| `scenario.json.policy` | 목적함수 순서, 동률 규칙, 허용/금지 변경 |
| `expected-results.json` | 주문별 기대 상태, 사유, 기본 배정과 대안 배정 |

## 5. `Order` 데이터 계약

```json
{
  "order_id": "ORD-004",
  "shipper_id": "SHP-02",
  "origin_terminal_ids": ["TRM-A"],
  "destination_terminal_ids": ["TRM-B"],
  "ready_at": "2026-08-17T09:30:00+09:00",
  "due_at": "2026-08-17T19:00:00+09:00",
  "gross_weight_kg": 18200,
  "dimensions_mm": {"length": 13600, "width": 2500, "height": 3900},
  "compatibility_tags": ["TRAILER_STANDARD"],
  "priority_class": "P3",
  "adjustment_window": null,
  "source_ref": {"source_type": "DEMO_ASSUMPTION", "assumption_id": "ASM-001"}
}
```

위 값은 정본 fixture의 `ORD-004`와 일치한다. `priority_class`는 `P1`·`P2`·`P3`만 쓰며(02 §10 vocabulary), 대안창이 없는 주문은 `adjustment_window = null`이다. 대안창이 있는 주문은 `{"alternative_service_ids": [...], "alternative_destination_terminal_ids": [...]}` 형태로 승인된 다음 운행·대체 터미널만 담는다(예: `ORD-005`, `ORD-008`).

화주 좌표는 지도 표시용이며 편성 제약에 사용하지 않는다. 정확한 주소·연락처·운임은 넣지 않는다.

## 6. 정본 주문 장면

| 주문 | 의도된 장면 | 기본안 기대 결과 |
| --- | --- | --- |
| `ORD-001` | 정상·고우선순위 | 배정 |
| `ORD-002` | 정상·임박 납기 | 배정 |
| `ORD-003` | 정상 | 배정 |
| `ORD-004` | 조건 충족, 슬롯 경합 | `ELIGIBLE + UNASSIGNED`, `CAPACITY_CONFLICT` |
| `ORD-005` | 기본 반입 마감 초과 | 다음 운행 대안에서 배정 가능 |
| `ORD-006` | 총중량 누락 | `REVIEW_REQUIRED` |
| `ORD-007` | 터널 또는 슬롯 높이 초과 | `INELIGIBLE + NONE` |
| `ORD-008` | 기본 터미널 취급 불가 | 대체 터미널 대안 가능 |
| `ORD-009` | 다음 운행도 납기 초과 | `INELIGIBLE + NONE` |

## 7. 데모 정책 v1

이 정책은 실제 기관 규칙이 아닌 목업 가정이다.

1. 배정 주문 수 최대화
2. `priority_class` 높은 주문 우선
3. `due_at`까지 여유가 작은 주문 우선
4. 주문 ID와 슬롯 ID 순으로 동률 해소

허용 대안은 `다음 운행 추가`와 사전 등록된 `대체 터미널 선택`이다. 반입 시각 조정은 이번 정본 정책에서 제외한다. 확률·교통 예측·"95% 성공" 같은 수치는 데이터 근거가 없으므로 목업에 포함하지 않는다.

## 8. 구현용 정본 fixture

제안용 파일 분할보다 구현·테스트의 일관성이 중요하므로, 최종 정본은 아래 두 JSON 파일이다.

- `fixtures/canonical-v1/scenario.json` — 5개 가상 화주, 3개 터미널, 3개 운행, 3개 화차, 7개 슬롯, 9개 주문, 정책과 가정
- `fixtures/canonical-v1/expected-results.json` — 기본 실행 설정·입력/정책/결과 hash, 기본 배정 3건과 주문 9건의 기대 상태, 주문별 대안 기대 결과

### 변하지 않는 데모 값

| 항목 | 값 | 검증 목적 |
| --- | --- | --- |
| 기본 운행 | `SVC-AM-01`, 12:00 출발·16:00 도착·10:30 마감 | 시간·경합 |
| 기본 슬롯 | `SLT-AM-01`~`03` | 3개 슬롯만 제공 |
| 표준 한도 | 높이 4,000mm·중량 24,000kg | 규격·중량 제약 |
| 대안 다음 운행 | `SVC-NEXT-01`, 17:00 출발 | `ORD-005` 대안 |
| 대안 도착 터미널 | `TRM-C`와 `SVC-AC-01` | `ORD-008` 대안 |

`shippers[].pickup_geo`와 `delivery_geo`는 지도 선을 그리기 위한 가상 좌표일 뿐, 후보 터미널이나 도로시간을 계산하는 입력이 아니다.

## 9. 대안 후 기대 상태

`expected-results.json.alternatives`는 배정 슬롯만 기록하지 않는다. 대안을 탐색한 뒤 기본 결과에 반영할 **완전한** `baseline_order_update`와 파생 실행의 `alternative_run_order_outcome`을 함께 고정한다. 두 객체는 `order_id`, `input_state`, `eligibility_state`, `assignment_state`, `alternative_state`, `primary_reason_code`를 모두 가진다. 성공 대안은 요청·HTTP 201·파생 실행 ID·`assignment_deltas`를, 대안 없음은 요청·HTTP 200·`NO_FEASIBLE_ALTERNATIVE` 사유를 함께 기록한다.

- `ORD-005`, `ORD-008` → `alternative_state = AVAILABLE`, 배지 `조건부 대안 있음`
- `ORD-007`, `ORD-009` → `alternative_state = NONE`, 배지 없음

따라서 UI는 기본안의 `NOT_SEARCHED`와 대안 탐색 후 `AVAILABLE`·`NONE`을 혼동하지 않는다.

`negative_api_cases.TC-10`은 금지된 `CHANGE_ROUTE_CLEARANCE` 요청이 `409 POLICY_VIOLATION`인지 검증한다. 이는 ORD-007의 “허용 대안 없음”과 별개의 API 계약 테스트다.
