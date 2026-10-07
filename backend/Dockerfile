# Built and run on the VPS (x86_64), so no cross-compilation is involved.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 app

WORKDIR /srv
COPY --from=builder /srv/.venv /srv/.venv
COPY app ./app

ENV PATH="/srv/.venv/bin:$PATH" \
    GEO_DATA_DIR=/data \
    GEO_DATABASE=/data/geo.db \
    GEO_CORS_ORIGINS="https://geo.lverma.com,https://geo-measure-web.vercel.app"

RUN mkdir -p /data && chown -R app:app /data
USER app
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health/', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
