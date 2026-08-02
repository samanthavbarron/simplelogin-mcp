# Alpine is viable here because every dependency ships musllinux wheels for
# amd64 and arm64. If a dependency without one is ever added, this build will
# start compiling from source -- switch to python:3.13-slim rather than
# installing a toolchain.
FROM python:3.13-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

# Build our own wheel first, then install it with source builds disabled. That
# keeps the guard where it matters -- on dependencies -- since a silent source
# build is how an Alpine image quietly becomes slow and fragile.
RUN uv build --wheel --out-dir /build/dist \
    && uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --only-binary=:all: /build/dist/*.whl


FROM python:3.13-alpine

LABEL org.opencontainers.image.title="simplelogin-mcp" \
      org.opencontainers.image.description="HTTP MCP server for the SimpleLogin alias API" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/samanthavbarron/simplelogin-mcp"

RUN addgroup -S app && adduser -S -G app -h /home/app app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER app
WORKDIR /home/app
EXPOSE 8000

# Exec form to sidestep shell quoting; honours MCP_PORT if it has been overridden.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; url='http://127.0.0.1:'+os.environ.get('MCP_PORT','8000')+'/health'; sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"]

CMD ["simplelogin-mcp"]
