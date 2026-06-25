# ── Stage 1: dependency builder ─────────────────────────────────────────────
# Installs Python packages into /root/.local so they can be copied to the
# runtime stage without dragging pip/wheel/build tools along.
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt requirements-whisper.txt ./
RUN pip install --user --no-cache-dir -r requirements.txt

# Optional local Whisper backend:
#   docker-compose build --build-arg INSTALL_WHISPER=true
ARG INSTALL_WHISPER=false
RUN if [ "$INSTALL_WHISPER" = "true" ]; then \
        pip install --user --no-cache-dir -r requirements-whisper.txt; \
    fi


# ── Stage 2: runtime image ───────────────────────────────────────────────────
# Only runtime system deps (ffmpeg, curl). Build tools are NOT present here.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system appuser && adduser --system --ingroup appuser appuser

WORKDIR /app

# Copy Python packages installed by the builder stage
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH

# Optional headless browser for JS-rendered/paywalled page scraping (heavy —
# pulls in Chromium + system libs). Enable the "Scraping & Audio → JavaScript-
# Seiten rendern" setting after building with:
#   docker-compose build --build-arg INSTALL_BROWSER=true
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        pip install --no-cache-dir playwright && \
        playwright install --with-deps chromium; \
    fi

COPY --chown=appuser:appuser app/ ./app/

RUN mkdir -p /app/data /app/downloads && chown -R appuser:appuser /app/data /app/downloads

USER appuser

EXPOSE 7878

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:7878/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7878", "--workers", "1"]
