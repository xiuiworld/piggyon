# Fly.io 배포용 이미지. Render 블루프린트가 쓰던 파이썬 버전을 그대로 고정한다.
# 리전은 fly.toml 이 정한다 — nrt(도쿄)다. Fly 에 icn 은 없다.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 의존성을 먼저 깔아야 앱 코드만 바뀐 재배포에서 이 레이어가 캐시된다.
# ortools 는 pandas 까지 끌고 오는 무거운 휠이라 캐시 여부가 빌드 시간을 좌우한다.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# app/canonical.py 가 <repo_root>/data/canonical-v1 을 런타임에 읽는다.
# app/ 이 /srv/app 이므로 data/ 는 반드시 /srv/data 여야 한다.
COPY app ./app
COPY data ./data

# 첫 요청이 .pyc 생성을 떠안지 않도록 빌드 때 미리 컴파일한다.
RUN python -m compileall -q app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
