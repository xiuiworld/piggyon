# Rail Slot Planning MVP

피기백 철도 슬롯 편성 MVP. 운영자가 주문을 **편성 가능 / 확인 필요 / 불가**로 판정하고,
불가한 주문에 대해 **무엇을 바꾸면 다시 검토할 수 있는지**를 보여준다.

> 이 저장소의 모든 운영 수치는 `DEMO_ASSUMPTION` 목업이다. 실제 운행 가능성이나
> 운영 성과의 근거로 사용하지 않는다.

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

## 현재 구현 범위 — P2까지

| 페이즈 | 상태 |
| --- | --- |
| P0 뼈대 (스캐폴드 · 스냅샷 모델 · `POST /scenarios`) | 완료 |
| P1 입력 검증 + 적합성 게이트 | 완료 |
| P2 기본 편성 (CP-SAT) + 독립 검증기 | 완료 |
| P3 조건부 대안 | 미착수 |
| P4 생성형 AI 레이어 | 미착수 |
| P5 결정 · 저장 · 조회 | 미착수 |
| P6 통합 · 데모 하드닝 | 미착수 |

### 지금 살아 있는 엔드포인트

| 메서드·경로 | 응답 |
| --- | --- |
| `POST /v1/scenarios` | `201` + `scenario_id`, `state=VALIDATION_REQUIRED` |
| `POST /v1/scenarios/{id}/validate` | 주문별 `input_state`·`eligibility_state`·사유·후보 슬롯 |
| `POST /v1/scenarios/{id}/runs` | `201` + 배정, 주문 결과, 재현성 해시 |
| `GET /v1/runs/{id}` | 저장된 실행 결과 |
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

납기는 `arrival_at + 도착 터미널 minimum_handling_minutes ≤ due_at`이다(02 §5).

### 재현성

`reproducibility` 해시는 07 §8 정규화로 실제 계산한다. `expected-results.json`의 해시
값은 임의값이므로(09 §3) 비교 대상이 아니다. 같은 입력·seed 7·worker 1이면 같은
`result_sha256`이 나오는지만 검증한다.

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
  services/
    planning.py      validate/run 오케스트레이션
  routers/
    scenarios.py     POST /v1/scenarios, /validate, /runs
    runs.py          GET /v1/runs/{id}
data/canonical-v1/   당일 레포에서 재작성한 정본 입력
scripts/smoke.py     P0~P2 게이트 확인
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
가리키면 된다. `SUPABASE_URL`·`SUPABASE_KEY`는 `sync: false`라 Render가 값을 물어보고,
저장소에는 들어가지 않는다.

무료 플랜 웹 서비스는 유휴 시 슬립에 들어가 첫 요청이 느리다. 심사 기간 동안 URL이
살아 있어야 한다면(08) 유료 인스턴스나 외부 핑을 고려한다.

## 참고 문서

계약의 정본은 `docs/openapi.yaml`이다. `docs/01`~`09`가 요구사항·상태 모델·데이터
계약·아키텍처·테스트·데모 흐름·구현 계획을 담는다.
