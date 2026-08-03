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

# Install only the reviewed, exact production dependency graph. Hash checking
# prevents an altered distribution file from silently entering the image, and
# copying the lock first preserves Docker's expensive dependency cache layer.
COPY requirements.lock requirements.lock

RUN python -m pip install --require-hashes -r requirements.lock

# Only runtime code and the reviewed Sepolia deployment artifact enter the
# image. Local environment files and trained model files are excluded by
# .dockerignore; the model is mounted read-only when Compose starts the worker.
COPY apps/__init__.py apps/__init__.py
COPY apps/backend apps/backend
COPY apps/listener apps/listener
COPY packages packages
COPY apps/contracts/ignition/deployments/sepolia-security-audit-v1 \
    apps/contracts/ignition/deployments/sepolia-security-audit-v1

# A compromised web request or claim payload should not obtain root privileges
# inside the container. The listener's writable checkpoint is prepared by a
# small one-off init service in Compose.
RUN useradd --create-home --uid 10001 claims
USER claims

EXPOSE 8000 9101 9102

# Compose supplies a different command for each process that shares this image.
CMD ["python", "-m", "apps.backend.app.main"]
