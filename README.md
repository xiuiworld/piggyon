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

P0 게이트를 실행 중인 서버에 대고 확인한다:

```bash
python scripts/smoke_p0.py
```

## 현재 구현 범위 — P0

| 페이즈 | 상태 |
| --- | --- |
| P0 뼈대 (스캐폴드 · 스냅샷 모델 · `POST /scenarios`) | 완료 |
| P1 입력 검증 + 적합성 게이트 | 미착수 |
| P2 기본 편성 (CP-SAT) | 미착수 |
| P3 조건부 대안 | 미착수 |
| P4 생성형 AI 레이어 | 미착수 |
| P5 결정 · 저장 · 조회 | 미착수 |
| P6 통합 · 데모 하드닝 | 미착수 |

### 지금 살아 있는 엔드포인트

| 메서드·경로 | 응답 |
| --- | --- |
| `POST /v1/scenarios` | `201` + `scenario_id`, `state=VALIDATION_REQUIRED` |
| `GET /health` | 저장소 백엔드와 도달 가능 여부 |

실패 응답은 모두 `code`·`message`·`details`·`trace_id`를 가진다. 스키마 위반은
`400 INVALID_INPUT`이며, `422`는 `VALIDATION_REQUIRED` 전용으로 남겨 둔다.

## 구조

```text
app/
  main.py            FastAPI 앱, 예외 핸들러, OpenAPI 보정
  config.py          환경변수 설정
  storage.py         Store 프로토콜 + MemoryStore / SupabaseStore
  errors.py          Error 계약 (04 §1, §9)
  canonical.py       정본 시나리오 로더
  models/
    snapshot.py      ScenarioInputSnapshot (openapi.yaml의 타입 미러)
    api.py           요청·응답 모델
  routers/
    scenarios.py     POST /v1/scenarios
data/canonical-v1/   당일 레포에서 재작성한 정본 입력
scripts/smoke_p0.py  P0 게이트 확인
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
