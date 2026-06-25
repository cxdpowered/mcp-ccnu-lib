# Playwright 官方 python 镜像已内置 Chromium + 全部系统依赖。
# tag 版本必须与 pyproject.toml 的 playwright==x.y.z 完全一致，否则浏览器二进制对不上会崩。
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data \
    HEADLESS=true \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8010

EXPOSE 8010
CMD ["python", "-m", "ccnu_lib.server"]
