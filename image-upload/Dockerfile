FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./
COPY image_upload ./image_upload

RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "image-upload"]
