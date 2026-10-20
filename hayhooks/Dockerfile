FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY pipelines ./pipelines
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 1416
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1416/status')"]

CMD ["/app/.venv/bin/hayhooks", "run", "--host", "0.0.0.0", "--port", "1416", "--pipelines-dir", "/app/pipelines"]
