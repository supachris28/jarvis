# --- web: marked + DOMPurify for the chat's Markdown, vendored (the CSP only allows scripts from Jarvis itself) ---
FROM node:22-alpine AS web
WORKDIR /w
RUN npm pack --silent marked@18 dompurify@3 \
    && mkdir -p vendor && for f in *.tgz; do tar -xzf "$f"; mv package "${f%%-[0-9]*}"; done \
    && cp marked/lib/marked.umd.js dompurify/dist/purify.min.js vendor/ \
    && (cp marked/LICENSE* vendor/ 2>/dev/null; cp dompurify/LICENSE* vendor/ 2>/dev/null; true)

# --- base: dependencies and code (shared by the test run and the final image) ---
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    JARVIS_DATA_DIR=/data \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# Tesseract reads text from screenshots shared to Jarvis
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
# The container runs read-only, so Python can never write .pyc files at runtime: compile once here instead,
# or every start compiles everything.
RUN python -m compileall -q /app/src

# --- test: the whole suite against the exact libraries in the image ---
# scripts/deploy.sh builds this target first and stops if anything fails. `docker build --target test .`
FROM base AS test
COPY tests ./tests
RUN PYTHONPATH=/app/src:/app/tests python -m unittest discover -s tests

# --- final image ---
FROM base AS final
COPY --from=web /w/vendor/ /app/src/jarvis/web/static/vendor/
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin jarvis \
    && mkdir -p /data && chown jarvis:jarvis /data

USER jarvis
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "-m", "jarvis.web.app"]
