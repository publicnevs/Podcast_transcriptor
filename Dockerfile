FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional local Whisper backend:
#   docker-compose build --build-arg INSTALL_WHISPER=true
ARG INSTALL_WHISPER=false
RUN if [ "$INSTALL_WHISPER" = "true" ]; then \
        pip install --no-cache-dir -r requirements-whisper.txt; \
    fi

COPY app/ ./app/

RUN mkdir -p /app/data /app/downloads

EXPOSE 7878

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:7878/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7878", "--workers", "1"]
