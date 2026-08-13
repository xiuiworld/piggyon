# 09. 구현 계획 (해커톤 당일)

> 버전: v1.0 · 기준일: 2026-08-13 · 상태: **당일 실행 계획**

## 0. 전제

- 본선 당일(9–18시)에 전량 구현한다. 시간표가 아니라 **의존성 순서 페이즈**로 나눈다.
- 규정: **레포는 당일 생성**, 당일 커밋만 평가, 사전 코드 복붙·커밋 금지. `docs/`·`fixtures/`·노션은 **참고 자료**이므로, 코드·스키마·목업은 당일 레포에서 **다시 작성**한다(그 작업 자체가 당일 구현).
- API 키는 `.env`, 커밋 금지.
- 스택: Python + FastAPI + Pydantic / OR-Tools CP-SAT / Supabase(Postgres) / 프론트 Next.js(별도 담당).
- 각 페이즈 끝의 **게이트**는 정본 fixture(`fixtures/canonical-v1/`)로 판정한다. 게이트 통과 = 다음 페이즈, 실패 = 컷 규칙(§8) 적용.

## 1. P0 — 뼈대

- 목표: 데이터가 한 바퀴 흐른다.
- 구현: 레포 생성, FastAPI 스캐폴드, `ScenarioInputSnapshot` Pydantic 모델, Supabase 연결, `scenario.json` 재작성, `POST /scenarios`.
- 게이트: fixture를 받아 저장하고 `scenario_id`를 반환한다. `uvicorn` 구동 + Supabase ping OK.
- 산출: 프론트 담당이 API 계약을 확인하고 병렬 착수할 수 있다.

## 2. P1 — 입력 검증 + 적합성 게이트

- 목표: 노션의 `확인 필요`와 `불가` 상태를 만든다(데모 장면 1·2).
- 구현:
  - 입력 검증(필수값 누락·단위·참조·시간 모순) → `input_state`(`VALID`/`REVIEW_REQUIRED`).
  - 하드 제약 판정(반입 마감, 납기, 규격 높이·폭·길이, 중량, 터미널 취급 태그, 경로 클리어런스) → `eligibility_state`.
  - `primary_reason_code`는 `02 §6.1` 우선순위(`INPUT_ > TIME_ > TERMINAL_ > DIMENSION_/WEIGHT_ > RESOURCE_ > CAPACITY_ > ALTERNATIVE_`)로 결정.
  - 적합 주문마다 기본 운행(`SVC-AM-01`)의 후보 슬롯 목록 생성.
- 게이트(`POST /scenarios/{id}/validate`):
  - `ORD-006` = REVIEW_REQUIRED / MISSING_REQUIRED_FIELD
  - `ORD-007` = TUNNEL_HEIGHT_EXCEEDED, `ORD-008` = TERMINAL_NOT_COMPATIBLE
  - `ORD-005` = READY_AFTER_CUTOFF, `ORD-009` = DUE_TIME_EXCEEDED
  - `ORD-001~004` = 후보 슬롯 있음

## 3. P2 — 기본 편성 (CP-SAT)

- 목표: `편성 가능` / `편성 가능·미배정`을 만든다(데모 장면 3, 차별점의 앞단).
- 구현:
  - 결정 변수: 주문×슬롯 배정. 제약: 주문당 ≤1, 슬롯당 ≤1, P1의 하드 제약.
  - 목적함수 4단계 사전순: 배정 건수 최대 → 우선순위 점수 최대 → 납기 시각 최소 → 주문·슬롯 사전순.
  - `random_seed=7`, `num_search_workers=1`.
  - `assignment_state` + 미배정 사유(`CAPACITY_CONFLICT`).
- 게이트(`POST /scenarios/{id}/runs`):
  - `ORD-001·002·003` 배정(SLT-AM-01~03), `ORD-004` = UNASSIGNED + CAPACITY_CONFLICT
  - `expected-results.baseline.assignments`와 일치
- 주의: `result_sha256` 등 고정 해시는 임의값이다. **해시 매칭은 스코프에서 뺀다.** 필요하면 엔진 출력으로 fixture를 갱신한다.

## 4. P3 — 조건부 대안

- 목표: 노션 §1 "무엇을 바꾸면 다시 검토할 수 있는지"(핵심 차별, 데모 장면 4).
- 구현:
  - 허용 변경만 적용한 파생 시나리오(`parent_scenario_id` + `change_set`) 생성 후 재실행.
  - 허용: `ADD_ORDER_APPROVED_SERVICE`(다음 운행), `CHANGE_TO_APPROVED_TERMINAL`(대체 터미널). 금지: 중량·규격·클리어런스·납기 변경.
  - 성공 시 `impacted_order_ids` + `assignment_deltas`(before/after), 기본 결과의 `alternative_state`만 갱신(주 상태 보존).
- 게이트(`POST /runs/{id}/alternatives`):
  - `ORD-005`(다음 운행)·`ORD-008`(대체 터미널) → 201 + deltas
  - `ORD-007`·`ORD-009` → 200 NO_FEASIBLE_ALTERNATIVE, alternative_state=NONE
  - `CHANGE_ROUTE_CLEARANCE` 직접 요청 → 409 POLICY_VIOLATION

## 5. P4 — 생성형 AI 레이어

- 목표: AI 대회 대응. 눈에 보이는 생성형 AI 지점을 만든다. 편성 판정은 여전히 CP-SAT가 한다.
- 구현(둘 중 최소 (a)):
  - (a) **인테이크**: 비정형 의뢰서 텍스트 → order JSON 구조화, 누락값을 `확인 필요`로 분류. LLM 호출 1회.
  - (b) **설명**: 검증된 결과(`order_outcomes`·대안)만 입력받아 운영자용 문장 카드 생성. 주문 ID·상태·사유·수치가 원본과 일치하는지 대조, 스키마 밖 사실 생성 금지.
- 게이트: 샘플 의뢰서 → 구조화 주문 + 누락 1건 플래그. 결과 → 자연어 설명.
- LLM: **OpenAI(GPT) 사용** — 팀 크레딧이 OpenAI에 있음. `openai` 라이브러리 + `OPENAI_API_KEY`. `anthropic`도 설치돼 있으나 크레딧 없어 미사용(대체용으로만 보관).
- 의존: `OPENAI_API_KEY`(`.env`), 데모용 샘플 의뢰서 텍스트.

## 6. P5 — 결정 · 저장 · 조회

- 목표: 운영자 결정(데모 장면 5).
- 구현: `POST /runs/{id}/decisions`(ACCEPTED는 OPTIMAL+PASS만), Supabase 저장, `GET /runs/{id}`, 여유 시 `GET /runs/{id}/export`.
- 게이트: 결정 저장·조회, `FEASIBLE`은 ACCEPTED 요청 시 409 RUN_NOT_ACCEPTABLE.

## 7. P6 — 통합 · 데모 하드닝

- 목표: 브라우저에서 5장면이 끝까지 흐른다.
- 구현: 프론트 계약 정합, `DEMO_ASSUMPTION` 배지, 발표 금지문구 점검(08 §8), 배포(URL 확보·8/21까지 유지).
- 게이트: 데모 5장면 클릭 통과 + 배포 URL 응답.

## 8. 컷 규칙 (시간 부족 시)

- **스파인 = P1·P2·P3.** 이게 제품의 핵심·차별·대안. 무조건 사수.
- **P4는 AI 설득력.** 스파인 다음 우선. (a)만이라도.
- **P5 저장은 인메모리로 강등 가능.** DB 없이도 데모는 돈다.
- 버리는 순서(급하면): export → decisions 상태전이 검증 → 독립 Plan Validator 재검산 → 18테스트 자동화 → 해시 재현.
- P2가 정오까지 안 끝나면 P3보다 P2 안정화를 우선한다. 대안 없이도 기본 편성만 있으면 발표 가능, 그 역은 아니다.

## 9. 병렬화

- 프론트: P0의 API 계약이 나오면 목업 응답으로 화면 6종 선행 개발. P1~P3 응답이 실제로 붙는 건 통합(P6).
- 백엔드: P1~P3는 순차(의존). P4·P5는 P3 이후 독립적으로 붙일 수 있다.

## 10. 당일 전 사전 준비 (환경·인프라 — 코드 아님, 합법)

- [ ] `python -c "import ortools"` OK (Windows 설치 확인)
- [ ] Supabase 프로젝트 생성 + connection string / key 확보
- [ ] `OPENAI_API_KEY` 준비(팀 크레딧 있는 계정) — P4용. Claude는 크레딧 없어 미사용
- [ ] 데모용 비정형 의뢰서 샘플 텍스트 1~2개
- [ ] 배포 계정(프론트/백) 로그인 확인
- [ ] 팀 역할 분담 확정(규정: 전원 참여)
