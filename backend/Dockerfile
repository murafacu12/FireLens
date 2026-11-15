# FIRELENS API. Multi-architecture: builds on x86-64 and on Arm (Oracle Cloud Ampere A1).
# The database and every side file live on the volume mounted at /data.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FIRELENS_DATABASE_URL=sqlite:////data/firelens.db \
    FIRELENS_DATA_DIR=/data

WORKDIR /app

# rasterio's bundled GDAL needs libexpat from the system
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# dependencies first, so code changes don't reinstall them (rasterio, numpy and scipy ship
# binary wheels for amd64 and arm64: no compiler needed)
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y firelens-backend \
    && rm -rf app build *.egg-info

COPY app ./app

RUN useradd --system --uid 10001 --home /app firelens \
    && mkdir -p /data && chown firelens /data
USER firelens

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8)"

# One process: the hourly NASA refresh and history jobs run inside it (never run two).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
