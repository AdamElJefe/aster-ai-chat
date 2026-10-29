# API and worker share one image; the command selects the process.
# Pin the base image by digest at release time (supply-chain policy, spec H1).
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev
RUN useradd --system --uid 10001 aster
USER aster
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "--factory", "aster_api.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
