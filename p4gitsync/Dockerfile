# ---- Build stage ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    pkg-config \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install .

# ---- Runtime stage ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    git-lfs \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install

COPY --from=builder /install /usr/local

RUN useradd -r -m -s /bin/bash p4sync \
    && mkdir -p /data /app \
    && chown p4sync:p4sync /data /app

USER p4sync
WORKDIR /app

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["p4gitsync"]
CMD ["--config", "/app/config.toml", "run"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1
