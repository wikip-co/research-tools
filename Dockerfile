FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    AGENT_TOOLS_ROOT=/opt/content-agent-tools \
    CONTENT_REPO_ROOT=/workspace/content \
    GMAIL_READER_DB=/var/lib/content-agent/gmail-reader/scholar-alerts.db \
    PATH=/opt/content-agent-tools:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        jq \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.13.4
RUN npm install -g @googleworkspace/cli agent-browser@0.41.2
RUN agent-browser install --with-deps

WORKDIR /opt/content-agent-tools
COPY pyproject.toml uv.lock ./
COPY content-agent-core ./content-agent-core
COPY gmail-reader ./gmail-reader
COPY wiki-automation ./wiki-automation
COPY image-upload ./image-upload
COPY web-scraper ./web-scraper
COPY agent-workflow ./agent-workflow
COPY scripts ./scripts

RUN chmod +x /opt/content-agent-tools/agent-workflow /opt/content-agent-tools/scripts/*.sh \
    && uv sync --all-packages --frozen \
    && /opt/content-agent-tools/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["/opt/content-agent-tools/scripts/entrypoint.sh"]
CMD ["agent-workflow", "help"]
