# IntelliKnow KMS — backend API container (FastAPI + uvicorn).
# Runs the bot integrations, orchestrator, knowledge base, RAG, analytics,
# admin REST API, Telegram polling task, and monitoring publisher.
FROM python:3.11-slim

# Faster, quieter, reproducible pip behavior.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/intelliknow

# Install pinned dependencies first for layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application source only — admin UI and tests are not needed in this image.
COPY app/ ./app/

# Persistent data (SQLite, FAISS, uploads, credentials, log buffer) lives on
# a host bind mount at /data (see docker-compose.yml). Never baked into image.
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
