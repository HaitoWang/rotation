ARG PYTHON_BASE_IMAGE=python:3.11-slim-bookworm
ARG NODE_BASE_IMAGE=node:24-alpine

FROM ${NODE_BASE_IMAGE} AS frontend-build

WORKDIR /frontend
COPY source/webui/frontend/package.json source/webui/frontend/package-lock.json ./
RUN npm ci
COPY source/webui/frontend/ ./
RUN npm run build

FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENAI_SENTINEL_NODE_PATH=node \
    WEBUI_DB_PATH=/app/data/webui.db \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs \
    && update-ca-certificates \
    && test -r /etc/ssl/certs/ca-certificates.crt \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY source/requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && mkdir -p /app/data /app/outputs

# The repository keeps the runnable Python package below source/. Copy it
# after installing dependencies so source changes do not invalidate pip's
# cached image layer.
COPY source/ /app/
# Vite writes to /static because its outDir is ../static relative to /frontend.
COPY --from=frontend-build /static/ /app/webui/static/

EXPOSE 8765
CMD ["python", "start_webui.py", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
