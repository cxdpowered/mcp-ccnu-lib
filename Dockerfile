# 纯 HTTP 实现，无需浏览器 —— 多阶段构建，运行镜像只含装好的依赖，无 pip 缓存/构建产物。

# ---- builder：把包+依赖装进独立 prefix ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime：仅拷贝装好的依赖，非 root 运行 ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8010
COPY --from=builder /install /usr/local
RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app
WORKDIR /app
EXPOSE 8010
CMD ["python", "-m", "ccnu_lib.server"]
