ARG PYTHON_BASE_IMAGE=python:3.11-slim-bookworm
ARG NODE_BASE_IMAGE=node:24-alpine

FROM ${NODE_BASE_IMAGE} AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENAI_SENTINEL_NODE_PATH=node \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    PYTHONPATH=/app/backend \
    REGERT_DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs \
    && update-ca-certificates \
    && test -r /etc/ssl/certs/ca-certificates.crt \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && mkdir -p /app/data /app/outputs

COPY deploy/alembic.ini /app/alembic.ini

# The repository keeps the runnable Python package below backend/. Copy it
# after installing dependencies so source changes do not invalidate pip's
# cached image layer.
COPY backend/ /app/backend/
COPY --from=frontend-build /frontend/dist/ /app/frontend/dist/

EXPOSE 8765
CMD ["python", "-m", "app.main"]
