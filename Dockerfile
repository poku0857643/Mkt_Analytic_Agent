FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Cloud Run: audit records to stdout -> Cloud Logging.
    AUDIT_LOG_PATH=-

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .
COPY app/ app/

RUN useradd --create-home --uid 10001 appuser
USER appuser

# Cloud Run sets PORT; default to 8080 elsewhere.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
