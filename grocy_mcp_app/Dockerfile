# Home Assistant app image for the Grocy MCP server.
# Build context is the REPOSITORY ROOT (see .github/workflows/ha-addon.yml):
#   docker build -f grocy_mcp_app/Dockerfile .
FROM python:3.12-slim

ARG BUILD_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8100 \
    MCP_PATH=/mcp \
    APP_VERSION=${BUILD_VERSION}

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install .

COPY grocy_mcp_app/run.py ./run.py

EXPOSE 8100

LABEL io.hass.type="addon" \
      io.hass.name="Grocy MCP Server" \
      io.hass.arch="amd64|aarch64" \
      io.hass.version="${BUILD_VERSION}"

CMD ["python", "/app/run.py"]
