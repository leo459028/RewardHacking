FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-otel.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# v1.6.0 D5: optional OpenTelemetry export; the app stays a no-op unless OTEL_ENABLED=true.
ARG INSTALL_OTEL=1
RUN if [ "$INSTALL_OTEL" = "1" ]; then pip install --no-cache-dir -r requirements-otel.txt; fi

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY tests ./tests
COPY README.md ./

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
