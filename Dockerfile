FROM node:24-bookworm-slim AS codex
RUN npm install --global @openai/codex@0.159.1

FROM python:3.11-slim-bookworm AS application
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MELODY_PDF_FONT=/app/static/fonts/NotoSansKR-400.ttf \
    MELODY_PDF_BOLD_FONT=/app/static/fonts/NotoSansKR-700.ttf
WORKDIR /app

COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-import-worker.txt ./
RUN python -m pip install -r requirements-import-worker.txt \
    && python -m pip check \
    && python -m playwright install --with-deps chromium

COPY app ./app
COPY static ./static
COPY feature_word_meaning ./feature_word_meaning
EXPOSE 8080

FROM application AS worker
CMD ["sh", "-c", "exec uvicorn feature_word_meaning.import_cloud:create_cloud_worker --factory --host 0.0.0.0 --port ${PORT:-8080}"]

FROM application AS web
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
