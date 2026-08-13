# 11. 배포 · Render → Fly.io 이전 근거

> 버전: v1.0 · 기준일: 2026-08-13 · 상태: **적용본**

## 1. 왜 옮겼나

Render 무료 티어에서 데모 응답이 느렸다. 원인을 추측하지 않고 배포본을 직접 쟀다
(서버가 **깨어 있는 상태**에서 측정).

**측정 방법 주의.** 요청마다 새 커넥션을 열면 TLS 핸드셰이크(첫 연결에서
300~400ms)가 값을 지배해서 엔드포인트 간 차이를 덮어버린다. 실제 브라우저와
프론트엔드는 커넥션을 재사용하므로, 아래 표는 **커넥션 재사용 기준 정상 상태**다.

| 엔드포인트 (서울에서 측정) | Render `sin` (이전) | Fly `nrt` (이후) |
| --- | --- | --- |
| `/docs` (DB 미접근) | 130~168ms | **45~50ms** |
| `/health` (Supabase 1회) | 258~271ms | **85~89ms** |

두 축이 각각 줄었다.

- **사용자 → 서버**: `/docs` 기준 130~168ms → 45~50ms. Fly 도쿄까지 TCP connect 가
  ~20ms 이므로 서버측 처리는 25~30ms 다.
- **서버 → Supabase**: 두 행의 차이가 DB 왕복 1회 비용이다.
  Render(싱가포르↔서울) **110~130ms**, Fly(도쿄↔서울) **~40ms**.

`/docs` 가 DB 를 안 쓰는데도 Render 에서 130~168ms 였다는 점이 원인 판정의 핵심이다.
같은 코드가 로컬 컨테이너에서 4.5ms 였으므로, 느림은 앱이 아니라 플랫폼(0.1 vCPU)과
지리(싱가포르 오리진)에서 왔다.

> 정정: 이 문서 초판은 "Supabase 왕복은 병목이 아니었다"고 적었다. 새 커넥션을
> 여는 방식으로 잰 탓에 핸드셰이크가 차이를 가린 결과였고, 실제로는 DB 왕복이
> 요청당 100ms 이상을 차지하고 있었다. 한 요청이 Supabase 를 여러 번 부르면
> 이 비용은 그만큼 누적된다.

콜드스타트는 여기에 더해진다. 정상 CPU 로컬에서 `import app.main`이 **1.40초**이고,
그중 `ortools.sat.python.cp_model`이 0.56초, 그 안에서 **pandas 가 0.44초**다
(ortools 가 pandas 를 끌고 온다). 0.1 vCPU 에서는 이 값이 10배 이상으로 늘어난다.

정리하면 세 겹이었다.

1. 15분 유휴 슬립 → 첫 요청이 무거운 임포트를 떠안음
2. 0.1 vCPU — CP-SAT 솔버가 이 위에서 돎 (`num_search_workers=1`로 이미 낮췄지만
   CPU 자체가 1/10)
3. 리전 불일치 — Supabase 는 서울(`ap-northeast-2`), Render 는 싱가포르

## 2. 무엇을 골랐나

Fly.io `nrt`(도쿄), `shared-cpu-1x` / 512MB, **상시 가동**, 머신 1개.

처음에는 `icn`(서울)로 잡았으나 **Fly 에 서울 리전은 없다.** 배포가
`Region 'icn' cannot host your machine` 으로 실패했고, `fly platform regions` 의
Asia Pacific 은 `bom`/`sin`/`syd`/`nrt` 뿐이다. 한국 최근접은 도쿄다.

- 전용 vCPU 1개를 요청 처리에 씀 — (2) 해소
- `auto_stop_machines = "off"` + `min_machines_running = 1` — (1) 해소
- (3) 리전 불일치는 **완전히는 해소되지 않았다.** 앱→Supabase 가 도쿄↔서울로
  남아 DB 왕복 1회당 **~40ms** 를 계속 낸다(싱가포르였을 때 110~130ms 에서
  줄기는 했다). 한 요청이 Supabase 를 여러 번 부르면 이 값이 누적되므로,
  DB 호출 수를 줄이는 것이 남은 최적화 여지다.

배포 시 `--ha=false` 를 반드시 붙인다. 기본값은 머신 2개이고, 상시 가동과
겹치면 비용이 그대로 2배가 된다.

상시 가동은 월 $3~4 수준이다. 데모 기간에는 심사 중 첫 요청이 콜드스타트를 맞는
것보다 이 비용이 싸다. 데모가 끝나면 `auto_stop_machines = "stop"` 으로 바꿔
유휴 시 비용을 0에 가깝게 내릴 수 있다(대신 콜드스타트가 돌아온다).

검토했지만 안 고른 것:

- **Render Starter $7/월** — 코드 변경 0이고 슬립도 사라지지만, 싱가포르 오리진이
  그대로라 §1 의 두 축(사용자 130~168ms, DB 왕복 110~130ms)이 남는다. 0.1 → 0.5
  vCPU 로 처리 시간만 줄어든다.
- **Cloud Run `asia-northeast3`(서울)** — 유일하게 서울에 실제로 있어서 DB 왕복
  ~40ms 까지 없앨 수 있다. 스케일 투 제로라 콜드스타트가 남는 게 데모에는
  안 맞아 보류했다. **DB 왕복이 병목으로 확인된 지금은 재검토할 가치가 있다** —
  Dockerfile 은 `CMD` 에서 포트를 `$PORT` 로 받게만 고치면 거의 그대로 쓴다.

## 3. 구성 파일

- [`Dockerfile`](../Dockerfile) — `python:3.13-slim` 고정. 로컬은 3.14.6 이지만
  런타임은 안정성을 위해 3.13 으로 의도적으로 낮춰 잡은 것이고, Render 블루프린트가
  쓰던 3.13.4 와도 같다. 이 간격은 의도된 것이므로 로컬에 맞춰 올리지 말 것.
  다만 3.14 전용 문법을 쓰면 배포에서만 깨지므로 그건 피한다.
- [`fly.toml`](../fly.toml) — 리전·머신 크기·헬스체크·동시성
- [`.dockerignore`](../.dockerignore) — `tests/`, `docs/`, `.env` 제외

`app/canonical.py` 가 런타임에 `<repo_root>/data/canonical-v1/scenario.json` 을
읽는다. 이미지에서 `app/` 이 `/srv/app` 이므로 `data/` 는 반드시 `/srv/data` 여야
한다. Dockerfile 의 `COPY data ./data` 를 지우면 부팅은 되고 요청에서만 깨진다.

동시성은 `soft_limit = 8` 로 낮춰 뒀다. CP-SAT 은 블로킹으로 돌고 vCPU 는 하나라,
동시 요청을 몰아주면 솔버끼리 CPU 를 뺏는다.

## 4. 시크릿

`fly.toml` 은 git 에 올라간다. 시크릿은 여기 두지 않는다.

```bash
fly secrets set SUPABASE_URL=... SUPABASE_KEY=... OPENAI_API_KEY=...
```

`fly.toml` 의 `[env]` 에는 비밀이 아닌 것만 둔다 (`STORAGE_BACKEND`,
`OPENAI_MODEL`).

`OPENAI_API_KEY` 는 없어도 된다. 없으면 P4 가 룰 기반 추출과 템플릿 문장으로
폴백하고 데모는 끝까지 돈다.

## 5. 이전 후 확인

```bash
python scripts/smoke.py https://piggyon-api.fly.dev
```

`render.yaml` 은 롤백 경로로 남겨 뒀다. 되돌리려면 Render 대시보드에서 블루프린트를
다시 켜고 README 의 URL 을 되돌리면 된다.
