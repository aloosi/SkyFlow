# syntax=docker/dockerfile:1

# ---- builder: resolve and build wheels once ---------------------------------
FROM python:3.12-slim AS builder
WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/skyflow.db

WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Non-root user; /data holds the SQLite file (mounted as a volume).
RUN useradd --system --create-home --uid 10001 skyflow \
    && mkdir -p /data && chown skyflow:skyflow /data
COPY --chown=skyflow:skyflow app ./app
COPY --chown=skyflow:skyflow scripts ./scripts
USER skyflow

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"

# Single worker: SQLite + in-process background tasks + per-flight locks assume one process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
