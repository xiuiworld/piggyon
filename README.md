# Rail Slot Planning MVP

피기백 철도 슬롯 편성 MVP. 운영자가 주문을 **편성 가능 / 확인 필요 / 불가**로 판정하고,
불가한 주문에 대해 **무엇을 바꾸면 다시 검토할 수 있는지**를 보여준다.

> 이 저장소의 모든 운영 수치는 `DEMO_ASSUMPTION` 목업이다. 실제 운행 가능성이나
> 운영 성과의 근거로 사용하지 않는다.

## 배포

- API: <https://piggyon-api.onrender.com>
- 문서: <https://piggyon-api.onrender.com/docs>
- 헬스체크: <https://piggyon-api.onrender.com/health>

저장소는 Supabase(`ap-northeast-2`), 배포는 Render 블루프린트다. 무료 인스턴스는
유휴 시 슬립에 들어가므로 첫 요청이 느릴 수 있다.

배포본 검증:

```bash
python scripts/smoke.py https://piggyon-api.onrender.com
```

## 실행

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --reload --port 8000
```

- API 문서: <http://127.0.0.1:8000/docs>
- 헬스체크: <http://127.0.0.1:8000/health>

## 테스트

```bash
python -m pytest tests/ -v
```

게이트를 실행 중인 서버에 대고 확인한다:

```bash
python scripts/smoke.py
```

## 현재 구현 범위

| 페이즈 | 상태 |
| --- | --- |
| P0 뼈대 (스캐폴드 · 스냅샷 모델 · `POST /scenarios`) | 완료 |
| P1 입력 검증 + 적합성 게이트 | 완료 |
| P2 기본 편성 (CP-SAT) + 독립 검증기 | 완료 |
| P3 조건부 대안 | 완료 |
| P4 생성형 AI 레이어 (인테이크 + 설명) | 완료 (키 없이도 동작) |
| P5 결정 · 저장 · 조회 · export | 완료 |
| P6 통합 · 배포 | 완료 (Supabase + Render, 배포본에서 5장면 통과) |

### 엔드포인트

| 메서드·경로 | 응답 |
| --- | --- |
| `POST /v1/scenarios` | `201` + `scenario_id`, `state=VALIDATION_REQUIRED` |
| `POST /v1/scenarios/{id}/validate` | 주문별 `input_state`·`eligibility_state`·사유·후보 슬롯 |
| `POST /v1/scenarios/{id}/runs` | `201` + 배정, 주문 결과, 재현성 해시 |
| `GET /v1/runs/{id}` | 저장된 실행 결과 |
| `POST /v1/runs/{id}/alternatives` | `201` 대안 + `assignment_deltas` / `200` 대안 없음 / `409` 금지 변경 |
| `POST /v1/runs/{id}/decisions` | `201` 결정 기록 (`ACCEPTED`는 `OPTIMAL`+`PASS`만) |
| `GET /v1/runs/{id}/export` | 입력·정책·결과·검증·결정·trace 한 묶음 |
| `POST /v1/intake/orders` | 비정형 의뢰서 → 주문 초안 + 누락 필드 |
| `GET /v1/runs/{id}/explanation` | 운영자용 상태 카드 |
| `GET /v1/ai/status` | 생성형 레이어 사용 가능 여부 |
| `GET /health` | 저장소 백엔드와 도달 가능 여부 |

실패 응답은 모두 `code`·`message`·`details`·`trace_id`를 가진다. 스키마 위반은
`400 INVALID_INPUT`이며, `422`는 `VALIDATION_REQUIRED` 전용으로 남겨 둔다.

### 정본 fixture에서 역산한 규칙 두 가지

문서가 두 가지로 읽히는 지점이 있어, `expected-results.json`이 성립하는 쪽으로 고정했다.

1. **반입 마감은 `ready_at ≤ planning_cutoff_at`이다.** 출발지 `minimum_handling_minutes`를
   더하지 않는다. 더하면 `ORD-008`(10:00 준비, 10:30 마감)이 `READY_AFTER_CUTOFF`가 되고,
   `TIME_ > TERMINAL_` 우선순위 때문에 기대값 `TERMINAL_NOT_COMPATIBLE`을 덮어쓴다.
2. **운행 단계에서 탈락하면 슬롯 단계를 보지 않는다.** 계속 내려가면 `ORD-007`에
   `SLOT_HEIGHT_EXCEEDED`가 붙고, 같은 `DIMENSION_` 계열 안의 사전순 동률 규칙에서
   기대값 `TUNNEL_HEIGHT_EXCEEDED`를 이겨버린다.
3. **승인된 운행은 요청 주문에만 열어 준다.** 파생 시나리오에서 `baseline_service_ids`를
   전역으로 넓히면 `ORD-004`가 `SLT-NEXT-01`을 가져가 영향 주문이 하나 더 생긴다.
   정본은 `ORD-005` 대안의 `impacted_order_ids`가 `["ORD-005"]` 하나다.

납기는 `arrival_at + 도착 터미널 minimum_handling_minutes ≤ due_at`이다(02 §5).

### 재현성

`reproducibility` 해시 3개는 `expected-results.json`의 값과 **정확히 일치**한다.
09 §3은 fixture 해시를 임의값이라 하고 해시 매칭을 스코프에서 뺐지만, 실제로는
셋 다 재현된다. 테스트가 fixture 값과 직접 대조한다.

두 가지가 결과를 가른다:

- **입력 해시는 "제출된 그대로"의 스냅샷을 덮는다.** 파싱된 모델을 다시 직렬화하면
  이쪽 기본값(`intake_cutoff_minutes: null`, 빈 `alternative_destination_terminal_ids`)이
  끼어들어 호출자가 보낸 적 없는 문서를 해싱하게 된다. 나중에 선택 필드를 하나
  추가하면 과거 해시가 전부 조용히 바뀐다는 문제도 있다.
- **`order_outcomes`는 5개 상태축만 해시에 넣는다.** `evidence`·`next_actions`는
  결정을 설명하는 표현이지 결정 자체가 아니다.

### 생성형 AI 경계

P4는 판정하지 않는다. 편성은 CP-SAT가 하고, LLM은 이미 검증된 JSON만 받아 문장으로
바꾼다. 생성된 카드는 서빙 전에 대조한다 — 이 실행에 없는 주문 ID나 사유 코드를
언급하거나, 08 §8이 금지한 주장(확률·% ·비용 절감·탄소·보장·실제 운행 가능)을 하면
템플릿 카드로 교체한다. 표시 라벨과 배지는 항상 계산값이고 생성 대상이 아니다.

`OPENAI_API_KEY`가 없으면 규칙 기반 추출과 템플릿 문장으로 내려간다. 05 §5가
요구하는 대로 키 없이도 데모 전체가 돈다.

## 구조

```text
app/
  main.py            FastAPI 앱, 예외 핸들러, OpenAPI 보정
  config.py          환경변수 설정
  storage.py         Store 프로토콜 + MemoryStore / SupabaseStore
  errors.py          Error 계약 (04 §1, §9)
  canonical.py       정본 시나리오 로더
  hashing.py         정규화·SHA-256 (07 §8)
  models/
    snapshot.py      ScenarioInputSnapshot (openapi.yaml의 타입 미러)
    api.py           요청·응답 모델
  rules/
    reason_codes.py  사유 코드와 primary 선정 규칙 (02 §6)
    eligibility.py   P1 입력 검증 + 적합성 게이트
  solver/
    baseline.py      P2 CP-SAT 사전순 다단 목적함수
  validation/
    plan_validator.py 솔버와 독립된 재검산 (05 §3)
  ai/
    client.py        OpenAI 호출 (없으면 None 반환)
    intake.py        P4(a) 비정형 의뢰서 구조화
    explain.py       P4(b) 설명 카드 + 사실 대조 가드
  services/
    planning.py      validate/run 오케스트레이션
    alternatives.py  P3 파생 시나리오·change_set·deltas
  routers/
    scenarios.py     POST /v1/scenarios, /validate, /runs
    runs.py          GET /runs/{id}, alternatives, decisions, export
    ai.py            intake, explanation, ai/status
data/canonical-v1/   당일 레포에서 재작성한 정본 입력
data/samples/        데모용 비정형 의뢰서 샘플
supabase/migrations/ Postgres 스키마
scripts/smoke.py     데모 5장면 게이트 확인
tests/               pytest
```

## 저장소 백엔드

`STORAGE_BACKEND=memory`(기본)는 자격증명 없이 동작한다. Supabase를 쓰려면
`.env.example`을 `.env`로 복사하고 `SUPABASE_URL`·`SUPABASE_KEY`를 채운 뒤
`STORAGE_BACKEND=supabase`로 바꾼다. 자격증명이 없거나 접속이 안 되면 경고를 남기고
인메모리로 내려간다(09 §8의 강등 허용).

스키마는 `supabase/migrations/`에 있다. 적용:

```bash
npx supabase@latest db push
```

## 배포 (Render)

`render.yaml` 블루프린트가 있다. Render 대시보드에서 **New → Blueprint**로 이 저장소를
가리킨다. `SUPABASE_URL`·`SUPABASE_KEY`·`OPENAI_API_KEY`는 `sync: false`라 Render가 값을
물어보고, 저장소에는 들어가지 않는다.

무료 플랜 웹 서비스는 유휴 시 슬립에 들어가 첫 요청이 느리다. 심사 기간 동안 URL이
살아 있어야 한다면(08) 유료 인스턴스나 외부 핑을 고려한다.

## 참고 문서

계약의 정본은 `docs/openapi.yaml`이다. `docs/01`~`09`가 요구사항·상태 모델·데이터
계약·아키텍처·테스트·데모 흐름·구현 계획을 담는다.
