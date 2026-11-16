# FIRELENS, complete: the website and the API in one container (Railway, or any Docker host).
# Railway builds this file from the repository (see railway.json and docs/RAILWAY.md).
# Local test:  docker build -t firelens . && docker run -p 8000:8000 -v firelens-data:/data firelens

# ---------------------------------------------------------------- 1. website (Vite + Cesium)
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# optional, for 3D terrain (Railway passes service variables to declared build arguments)
ARG VITE_CESIUM_ION_TOKEN=""
ENV VITE_CESIUM_ION_TOKEN=$VITE_CESIUM_ION_TOKEN \
    VITE_API_BASE=""
RUN npm run build

# ---------------------------------------------------------------- 2. API (FastAPI) + website
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FIRELENS_DATABASE_URL=sqlite:////data/firelens.db \
    FIRELENS_DATA_DIR=/data \
    FIRELENS_WEB_DIR=/app/web \
    FIRELENS_SEED_WINDOW=24h

WORKDIR /app

# rasterio's bundled GDAL needs libexpat from the system
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# dependencies first, so code changes don't reinstall them
COPY backend/pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y firelens-backend \
    && rm -rf app build *.egg-info

COPY backend/app ./app
COPY --from=web /web/dist ./web
RUN mkdir -p /data

# Runs as root: Railway mounts volumes owned by root, and the database lives on the volume.
EXPOSE 8000
# One process: the hourly NASA refresh and history loading run inside it (never run two copies).
# Railway sets PORT; elsewhere it defaults to 8000.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
