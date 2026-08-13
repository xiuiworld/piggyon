# 11. 배포 · Render → Fly.io 이전 근거

> 버전: v1.0 · 기준일: 2026-08-13 · 상태: **적용본**

## 1. 왜 옮겼나

Render 무료 티어에서 데모 응답이 느렸다. 원인을 추측하지 않고 배포본을 직접 쟀다
(서버가 **깨어 있는 상태**에서 측정).

| 엔드포인트 | TTFB | 비고 |
| --- | --- | --- |
| `/docs` (정적, DB 미접근) | 270~460ms | TCP connect 21~28ms |
| `/health` (Supabase 조회) | 367~391ms | |

두 값이 사실상 같다는 게 핵심이다. **Supabase 왕복은 병목이 아니었다.** DB를 전혀
건드리지 않는 정적 응답도 300~450ms가 나왔다. connect 25ms는 Cloudflare 서울
엣지까지의 값이고, 나머지 300~400ms는 엣지 → 싱가포르 오리진 왕복과 0.1 vCPU
위에서의 처리 시간이다.

콜드스타트는 여기에 더해진다. 정상 CPU 로컬에서 `import app.main`이 **1.40초**이고,
그중 `ortools.sat.python.cp_model`이 0.56초, 그 안에서 **pandas 가 0.44초**다
(ortools 가 pandas 를 끌고 온다). 0.1 vCPU 에서는 이 값이 10배 이상으로 늘어난다.

정리하면 세 겹이었다.

1. 15분 유휴 슬립 → 첫 요청이 무거운 임포트를 떠안음
2. 0.1 vCPU — CP-SAT 솔버가 이 위에서 돎 (`num_search_workers=1`로 이미 낮췄지만
   CPU 자체가 1/10)
3. 리전 불일치 — Supabase 는 서울(`ap-northeast-2`), Render 는 싱가포르

## 2. 무엇을 골랐나

Fly.io `icn`(서울), `shared-cpu-1x` / 512MB, **상시 가동**.

- 서울 리전이라 Supabase 와 같은 리전 — (3) 해소
- 전용 vCPU 1개를 요청 처리에 씀 — (2) 해소
- `auto_stop_machines = "off"` + `min_machines_running = 1` — (1) 해소

상시 가동은 월 $3~4 수준이다. 데모 기간에는 심사 중 첫 요청이 콜드스타트를 맞는
것보다 이 비용이 싸다. 데모가 끝나면 `auto_stop_machines = "stop"` 으로 바꿔
유휴 시 비용을 0에 가깝게 내릴 수 있다(대신 콜드스타트가 돌아온다).

검토했지만 안 고른 것:

- **Render Starter $7/월** — 코드 변경 0이고 슬립도 사라지지만, 싱가포르 오리진이
  그대로라 위에서 잰 300ms대 왕복이 남는다.
- **Cloud Run 서울** — 무료 한도가 크고 서울 리전도 대상이지만, 스케일 투 제로
  구조라 콜드스타트가 남고 데모에는 상시 가동이 더 맞았다.

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
