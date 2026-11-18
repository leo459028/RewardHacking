# v1.6.2: the image holds only what the application needs at run time. Secrets, keys, local configuration and tests
# stay out (.dockerignore allow list, checked by scripts/image_content_check.py). Update the base digest at each release.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt requirements-otel.txt ./

# Wheels only: no compiler is needed in either stage, and a dependency without a wheel fails the build loudly.
# v1.6.0 D5: optional OpenTelemetry export; the app stays a no-op unless OTEL_ENABLED=true.
ARG INSTALL_OTEL=1
RUN python -m venv --without-pip /opt/venv && \
    pip --python /opt/venv/bin/python install --no-cache-dir --only-binary=:all: -r requirements.txt && \
    if [ "$INSTALL_OTEL" = "1" ]; then \
        pip --python /opt/venv/bin/python install --no-cache-dir --only-binary=:all: -r requirements-otel.txt; \
    fi

COPY app /staging/app
COPY migrations /staging/migrations
COPY alembic.ini README.md LICENSE /staging/
COPY config/*.example.yaml /staging/config/
RUN find /staging -type d -exec chmod 755 {} + && find /staging -type f -exec chmod 644 {} +

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH=/opt/venv/bin:$PATH
ENV HOME=/tmp

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

# Root owns the program files, so the application user cannot change them.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /staging/ /app/

WORKDIR /app
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=3)"]

# RH_AUTO_MIGRATE=false: another instance or a one-off job runs the migration (see docs/deployment.md).
CMD ["sh", "-c", "set -e; if [ \"${RH_AUTO_MIGRATE:-true}\" = \"true\" ]; then alembic upgrade head; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
