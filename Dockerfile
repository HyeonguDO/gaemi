# 개미장부 in one container: the page, the daily broker sync and the 15-minute phone copy (`python -m assetlog run`).
# docker compose up -d, then open http://localhost:8000: the setup page asks for the keys. Everything it keeps
# (ledger, tokens, .env) is in the data/ volume.
FROM python:3.12-slim
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ASSETLOG_ENV_FILE=/app/data/.env
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY assetlog ./assetlog
USER 1000:1000
VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=5m --timeout=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/manifest.webmanifest')"
# 0.0.0.0 inside the container only: compose publishes the port on 127.0.0.1, so nothing outside this machine reaches it.
# Behind Docker's port publishing the app sees most clients as the bridge gateway, so its own check of the client's
# address can't tell them apart: the 127.0.0.1 publish is the boundary (Docker Engine 28+ keeps LAN neighbours off it).
CMD ["python", "-m", "assetlog", "run", "--host", "0.0.0.0"]
