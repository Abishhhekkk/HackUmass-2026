# RescueSight cloud API + dashboard in one image. Stateless: all data goes to DATABASE_URL
# (Tiger Data), so it runs on any container host or a laptop. Inference stays on the Jetson.
#
#   docker build -t rescuesight-api .
#   docker run --rm -p 8000:8000 --env-file .env rescuesight-api
#
# Hosts that assign the port set $PORT; default 8000.

# --- dashboard build -------------------------------------------------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- API ---------------------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY server/requirements.txt server/requirements-postgres.txt server/requirements-ai.txt server/
RUN pip install --no-cache-dir -r server/requirements.txt -r server/requirements-postgres.txt \
        -r server/requirements-ai.txt
COPY server/rescue_api server/rescue_api
COPY infra/db infra/db
COPY --from=web /web/dist web/dist
# SQLite fallback location when DATABASE_URL is not set (not persistent in a container)
RUN useradd --create-home --uid 10001 app && mkdir -p data/cloud && chown -R app data
USER app
WORKDIR /app/server
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/api/v1/health', timeout=4)"
CMD ["sh", "-c", "exec uvicorn rescue_api.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
