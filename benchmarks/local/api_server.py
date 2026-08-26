"""Loopback-only FastAPI application for the HTTP performance experiment."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import HTTPException, Request, status

from apps.backend.app.gasless_service import GaslessClaimSubmissionService
from apps.backend.app.main import (
    app,
    get_claimant_session,
    get_gasless_submission_service,
)
from apps.backend.app.submission_auth import ClaimAuthorizationSigner
from benchmarks.local.adapters import (
    BENCHMARK_AUTHORIZATION_KEY,
    BenchmarkEligibility,
    BenchmarkGaslessChain,
    BenchmarkPayloadStore,
    benchmark_database,
    benchmark_session,
)
from packages.integrations.postgres import PostgresGaslessSubmissionRepository


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required by the benchmark API")
    return value


@lru_cache
def benchmark_service() -> GaslessClaimSubmissionService:
    """Build real PostgreSQL orchestration over deterministic public services."""

    if os.environ.get("BENCHMARK_MODE") != "enabled":
        raise RuntimeError("Set BENCHMARK_MODE=enabled for the benchmark-only API")
    database = benchmark_database(
        _required_setting("BENCHMARK_DATABASE_URL"),
        _required_setting("BENCHMARK_SCHEMA"),
    )
    return GaslessClaimSubmissionService(
        ipfs=BenchmarkPayloadStore(),
        chain=BenchmarkGaslessChain(),
        store=PostgresGaslessSubmissionRepository(database),
        authorization=ClaimAuthorizationSigner(BENCHMARK_AUTHORIZATION_KEY),
        fingerprint_key=b"benchmark-request-fingerprint-key-v1",
        insurer_minute_limit=1,
        client_minute_limit=1,
        allow_rate_limit_bypass=True,
        eligibility=BenchmarkEligibility(),
    )


def benchmark_claimant(request: Request):
    """Authenticate an explicit local benchmark token, never a real session."""

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A benchmark bearer token is required",
        )
    try:
        return benchmark_session(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


@asynccontextmanager
async def benchmark_lifespan(_app):
    """Validate local-only configuration without loading Sepolia adapters."""

    benchmark_service()
    yield


# Reuse the production routes, request models, middleware, exception mapping and
# response models.  Override only the two dependencies that would otherwise
# authenticate a hosted session and construct public-network adapters.
app.dependency_overrides[get_claimant_session] = benchmark_claimant
app.dependency_overrides[get_gasless_submission_service] = benchmark_service
app.router.lifespan_context = benchmark_lifespan
