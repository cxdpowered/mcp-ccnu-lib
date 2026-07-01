# 纯 HTTP 实现，无需浏览器 —— 用轻量 python 镜像即可（不再依赖 Playwright/Chromium）。
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8010

EXPOSE 8010
CMD ["python", "-m", "ccnu_lib.server"]
