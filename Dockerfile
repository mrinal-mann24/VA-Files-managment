# va-bot — WhatsApp file-collection service (FastAPI)
FROM python:3.12-slim

# Don't write .pyc files; flush stdout/stderr immediately for container logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORAGE_ROOT=/data/storage

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Run as a non-root user and give it ownership of the storage volume.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/storage \
    && chown -R appuser:appuser /app /data
USER appuser

# Persisted per-client folders live here (see config.STORAGE_ROOT).
VOLUME ["/data/storage"]

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
