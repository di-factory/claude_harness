# dif-general-harness: one image per client instance.
#
# Build from the repository root with a staged solution (packs + instance), as
# `dif-general-harness deploy` prepares it under deploy/build/<instance-id>/:
#
#   docker build -f deploy/docker/Dockerfile \
#     --build-arg SOLUTION=deploy/build/<instance-id> -t <registry>/<instance-id>:<tag> .
#
# The image holds code and the approved spec only. Secrets come from the client's vault at
# runtime (DIF_SECRETS_BACKEND), data lives in Postgres (DIF_DB_* or DIF_DATABASE_URL).

ARG PYTHON=3.12

FROM python:${PYTHON}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra aws --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra aws

FROM python:${PYTHON}-slim
ARG SOLUTION=deploy/build/solution
RUN useradd --uid 10001 --create-home dif && mkdir -p /data && chown dif /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY ${SOLUTION} /app/solution
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DIF_SECRETS_BACKEND=aws-secrets-manager
USER dif
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
ENTRYPOINT ["dif-general-harness", "serve", "/app/solution/instance.json", "--packs", "/app/solution/packs", "--state", "/data"]
