# syntax=docker/dockerfile:1

# --- Stage 1: builder — установка зависимостей в изолированный venv ---
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /service

# venv в /opt/venv — переносим целиком в runtime-слой
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install -r requirements.txt

# --- Stage 2: runtime — минимальный образ без build-инструментов ---
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# non-root пользователь
RUN groupadd --system app && useradd --system --gid app --home-dir /service --no-create-home app

WORKDIR /service

# зависимости из builder-слоя
COPY --from=builder /opt/venv /opt/venv

# исходники приложения (app/api/templates и app/api/static входят в COPY app)
COPY app /service/app
COPY shared /service/shared
COPY scripts /service/scripts
COPY migrations /service/migrations
COPY alembic.ini /service/alembic.ini

# .env НЕ копируется в образ — секреты только через env/env_file в рантайме
RUN chown -R app:app /service

USER app

EXPOSE 8000

# health для оркестратора (compose переопределяет своим healthcheck)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]

CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120"]
