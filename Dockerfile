FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*
COPY requirements-web.txt requirements-web.lock ./
RUN pip install --no-cache-dir -r requirements-web.lock
COPY . .
RUN useradd --create-home --uid 10001 pipeline && chown -R pipeline:pipeline /app
USER pipeline
EXPOSE 8765
CMD ["uvicorn", "opportunity_app.api:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
