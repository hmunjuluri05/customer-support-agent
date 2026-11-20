FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 3000

# Single worker, no access-log buffering.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
