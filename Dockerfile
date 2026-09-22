FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /uvx /bin/
# Optional dependency groups: the capture worker image is built with EXTRAS=capture (Playwright client).
ARG EXTRAS=""

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Install locked dependencies first so source-only changes keep the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${EXTRAS:+--extra $EXTRAS}

COPY alembic.ini ./
COPY alembic ./alembic
COPY pensieve ./pensieve
RUN uv sync --frozen --no-dev --no-editable ${EXTRAS:+--extra $EXTRAS}


FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/Team-AER/pensieve" \
      org.opencontainers.image.description="A self-hosted RSS reader with an optional local-LLM layer" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 pensieve \
    && useradd --uid 10001 --gid pensieve --no-create-home --shell /usr/sbin/nologin pensieve

WORKDIR /app
COPY --from=builder --chown=pensieve:pensieve /app /app

USER pensieve
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["uvicorn", "pensieve.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
