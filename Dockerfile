FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8800

WORKDIR /app

# System deps: libsndfile1 for soundfile, curl for healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache)
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy application code
COPY . .

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=120s \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

EXPOSE 8800

ENTRYPOINT ["bash", "docker-entrypoint.sh"]
