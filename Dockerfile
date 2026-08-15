# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.24

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE=concierge \
    HOST=0.0.0.0 \
    PORT=8301
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
EXPOSE 8101 8202 8301
USER app:app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["sundae-funday-healthcheck"]
CMD ["sundae-funday-serve"]
