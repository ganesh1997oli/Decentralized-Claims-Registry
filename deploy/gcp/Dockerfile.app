# syntax=docker/dockerfile:1

# Python 3.13 matches the version used in continuous integration. Keeping the
# cloud and CI runtimes aligned avoids discovering version differences only
# after a deployment.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    MPLCONFIGDIR=/tmp/matplotlib \
    NUMBA_CACHE_DIR=/tmp/numba

WORKDIR /app

# XGBoost's Linux wheel uses the OpenMP runtime. Nothing else in this image
# needs a compiler because the Python dependencies publish binary wheels.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirement files before application code. Docker can then reuse the
# expensive dependency layer when only a Python function or comment changes.
COPY deploy/gcp/python-requirements.txt deploy/gcp/python-requirements.txt
COPY backend/requirements.txt backend/requirements.txt
COPY listener/requirements.txt listener/requirements.txt
COPY model/requirements.txt model/requirements.txt
COPY integrations/requirements.txt integrations/requirements.txt
COPY integrations/ipfs/requirements.txt integrations/ipfs/requirements.txt
COPY integrations/kafka/requirements.txt integrations/kafka/requirements.txt
COPY integrations/postgres/requirements.txt integrations/postgres/requirements.txt
COPY observability/requirements.txt observability/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r deploy/gcp/python-requirements.txt

# Only runtime code and the reviewed Sepolia deployment artifact enter the
# image. Local environment files and trained model files are excluded by
# .dockerignore; the model is mounted read-only when Compose starts the worker.
COPY backend backend
COPY duplicates duplicates
COPY integrations integrations
COPY listener listener
COPY model model
COPY observability observability
COPY contract/ignition/deployments/chain-11155111 \
    contract/ignition/deployments/chain-11155111

# A compromised web request or claim payload should not obtain root privileges
# inside the container. The listener's writable checkpoint is prepared by a
# small one-off init service in Compose.
RUN useradd --create-home --uid 10001 claims
USER claims

EXPOSE 8000 9101 9102

# Compose supplies a different command for each process that shares this image.
CMD ["python", "-m", "backend.app.main"]
