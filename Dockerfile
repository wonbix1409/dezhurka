FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dezhurka ./dezhurka
RUN useradd --uid 10001 --create-home dezhurka && mkdir -p /app/data && chown dezhurka /app/data
USER dezhurka

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# Один воркер: фоновые эскалации и бот должны жить в одном процессе
CMD ["uvicorn", "dezhurka.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
