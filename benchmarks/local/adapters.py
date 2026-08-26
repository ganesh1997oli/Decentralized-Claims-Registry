"""Deterministic boundaries shared by the single-host benchmark runners.

The benchmark keeps repository-owned work real: request validation, canonical
claim construction, hashing, PostgreSQL transactions, Kafka delivery,
duplicate detection, feature persistence, XGBoost and SHAP.  Only public or
paid dependencies are substituted.  These adapters must never be selected by
the production application.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from psycopg import sql
from psycopg.rows import dict_row
from web3 import Web3

from apps.backend.app.blockchain import ChainAssessment, ChainClaim
from apps.backend.app.claimant_auth import ClaimantSession
from apps.backend.app.gasless_blockchain import (
    GaslessBlockchainError,
    PreparedForwardRequest,
)
from apps.backend.app.policy_eligibility import ClaimantPrincipal
from packages.integrations.postgres import (
    PostgresDatabase,
    PostgresMigrator,
    PostgresRepositories,
)
from packages.model.contracts import FraudScore

BENCHMARK_CHAIN_ID = 31_337
BENCHMARK_CONTRACT = Web3.to_checksum_address(
    "0x1000000000000000000000000000000000000001"
)
BENCHMARK_FORWARDER = Web3.to_checksum_address(
    "0x2000000000000000000000000000000000000002"
)
BENCHMARK_INSURER = Web3.to_checksum_address(
    "0x3000000000000000000000000000000000000003"
)
BENCHMARK_PERMIT_ISSUER = Web3.to_checksum_address(
    "0x4000000000000000000000000000000000000004"
)
BENCHMARK_AUTHORIZATION_KEY = b"benchmark-claim-authorization-key-v1"
BENCHMARK_FINGERPRINT_KEY = b"benchmark-private-fingerprint-key-v1"

_SCHEMA = re.compile(r"claims_bench_[a-z0-9_]{8,56}\Z")
_TOKEN = re.compile(r"benchmark-[A-Za-z0-9._:-]{8,160}\Z")


def utc_now() -> datetime:
    """Return one timezone-aware clock value."""

    return datetime.now(UTC)


def normalize_hex(value: str) -> str:
    """Return a hexadecimal value with the prefix expected by API models."""

    return value if value.startswith("0x") else f"0x{value}"


def benchmark_account(token: str):
    """Derive a repeatable local-only wallet from a benchmark bearer token.

    This is intentionally unsuitable for real identities.  The benchmark API
    binds only to loopback and accepts tokens with an explicit benchmark prefix.
    Derivation lets the load process sign the exact EIP-712 response while the
    server independently reconstructs the expected address.
    """

    if not _TOKEN.fullmatch(token):
        raise ValueError("Invalid benchmark bearer token")
    private_key = hashlib.sha256(
        f"claims-benchmark-wallet-v1:{token}".encode()
    ).digest()
    return Account.from_key(private_key)


def benchmark_session(token: str) -> ClaimantSession:
    """Construct the claimant session used by the benchmark-only dependency."""

    account = benchmark_account(token)
    subject_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return ClaimantSession(
        subject_id=f"benchmark-{subject_digest}",
        claimant_address=account.address,
        expires_at=utc_now() + timedelta(hours=12),
    )


class BenchmarkPayloadStore:
    """Thread-safe content-addressed replacement for public Pinata/IPFS."""

    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def upload_bytes(
        self,
        payload: bytes,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        if not payload:
            raise ValueError("Benchmark payload cannot be empty")
        if not filename or not content_type:
            raise ValueError("Benchmark upload metadata cannot be empty")
        cid = hashlib.sha256(payload).hexdigest()
        with self._lock:
            self._payloads[cid] = payload
        return cid

    def put_pointer(self, pointer: str, payload: bytes) -> None:
        """Register an already named pointer for Kafka pipeline experiments."""

        if not pointer.startswith("ipfs://"):
            raise ValueError("Benchmark pointer must use ipfs://")
        with self._lock:
            self._payloads[pointer.removeprefix("ipfs://")] = payload

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        if attempts < 1:
            raise ValueError("attempts must be positive")
        key = pointer.removeprefix("ipfs://")
        with self._lock:
            try:
                return self._payloads[key]
            except KeyError as exc:
                raise ValueError(f"Unknown benchmark pointer {pointer!r}") from exc


class BenchmarkEligibility:
    """Return one isolated rate-exempt principal per verified local session."""

    configured_insurers = (("northstar-mutual", BENCHMARK_INSURER),)

    def verify(self, claim, session: ClaimantSession) -> ClaimantPrincipal:
        if claim.insurer_id != "northstar-mutual":
            raise ValueError("Benchmark claims must use northstar-mutual")
        return ClaimantPrincipal(
            subject_id=session.subject_id,
            claimant_address=session.claimant_address,
            submitter_address=session.claimant_address,
            claimant_commitment=normalize_hex(
                hashlib.sha256(session.subject_id.encode("utf-8")).hexdigest()
            ),
            insurer_id="northstar-mutual",
            insurer_address=BENCHMARK_INSURER,
            policy_id=f"policy-{session.subject_id[-32:]}",
            daily_quota=1_000_000,
            rate_limit_exempt=True,
        )


class BenchmarkGaslessChain:
    """Prepare and cryptographically verify requests without an RPC endpoint."""

    def __init__(self) -> None:
        self.deployment = SimpleNamespace(
            deployment_id="benchmark-local-v1",
            chain_id=BENCHMARK_CHAIN_ID,
            address=BENCHMARK_CONTRACT,
            forwarder_address=BENCHMARK_FORWARDER,
        )
        self._nonces: dict[str, int] = {}
        self._lock = threading.Lock()

    def permit_issuer_address(self, principal: ClaimantPrincipal) -> str:
        if principal.insurer_address.lower() != BENCHMARK_INSURER.lower():
            raise GaslessBlockchainError("Benchmark insurer address is invalid")
        return BENCHMARK_PERMIT_ISSUER

    def validate_principal(self, principal: ClaimantPrincipal) -> str:
        try:
            signer = Web3.to_checksum_address(principal.signer_address)
        except ValueError as exc:
            raise GaslessBlockchainError("Benchmark signer address is invalid") from exc
        self.permit_issuer_address(principal)
        return signer

    def prepare_request(
        self,
        *,
        principal: ClaimantPrincipal,
        claim_hash: bytes,
        data_pointer: str,
        permit_id: str | None,
    ) -> PreparedForwardRequest:
        signer = self.validate_principal(principal)
        if permit_id is None:
            raise GaslessBlockchainError("Benchmark public claim requires a permit ID")
        with self._lock:
            nonce = self._nonces.get(signer.lower(), 0)
            self._nonces[signer.lower()] = nonce + 1
        call_digest = Web3.keccak(
            claim_hash
            + data_pointer.encode("utf-8")
            + permit_id.encode("utf-8")
        ).hex()
        return PreparedForwardRequest(
            from_address=signer,
            to=BENCHMARK_CONTRACT,
            value=0,
            gas=400_000,
            nonce=nonce,
            deadline=int(utc_now().timestamp()) + 3_600,
            data=normalize_hex(call_digest),
        )

    def verify_signature(self, record, signature: str) -> None:
        request = PreparedForwardRequest.from_record(record)
        typed_data = request.typed_data(
            chain_id=record.chain_id,
            forwarder_address=record.forwarder_address,
        )
        try:
            signable = encode_typed_data(full_message=typed_data)
            recovered = Account.recover_message(signable, signature=signature)
        except Exception as exc:
            raise GaslessBlockchainError(
                "Benchmark EIP-712 signature could not be recovered"
            ) from exc
        if recovered.lower() != record.signer_address.lower():
            raise GaslessBlockchainError("Benchmark EIP-712 signature is invalid")


class BenchmarkAssessmentRegistry:
    """Idempotent in-memory substitute for the worker's Sepolia write."""

    def __init__(self) -> None:
        self._states: dict[int, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self.assessment_calls = 0

    def get_claim(self, claim_id: int) -> ChainClaim:
        with self._lock:
            status, fraud_score = self._states.get(claim_id, (0, 0))
        return ChainClaim(
            claim_id=claim_id,
            claimant="0x1111111111111111111111111111111111111111",
            claim_hash="0x" + ("00" * 32),
            data_pointer=f"ipfs://claim{claim_id}",
            status=status,
            fraud_score=fraud_score,
            submitted_at=1_750_000_000,
            updated_at=1_750_000_000,
        )

    def assess_claim(
        self,
        claim_id: int,
        status: int,
        fraud_score: int,
    ) -> ChainAssessment:
        with self._lock:
            existing = self._states.get(claim_id, (0, 0))
            if existing != (0, 0):
                raise ValueError(f"Benchmark claim {claim_id} is already assessed")
            self._states[claim_id] = (status, fraud_score)
            self.assessment_calls += 1
        return ChainAssessment(
            transaction_hash=f"0x{claim_id:064x}",
            block_number=10_000 + claim_id,
            status=status,
            fraud_score=fraud_score,
        )


class TimingScorer:
    """Measure real XGBoost/SHAP calls without changing their result."""

    def __init__(self, scorer: Any) -> None:
        self.scorer = scorer
        self._current = threading.local()
        self._durations: dict[str, float] = {}
        self._lock = threading.Lock()

    def bind(self, event_id: str) -> None:
        self._current.event_id = event_id

    def score(self, claim) -> FraudScore:
        event_id = getattr(self._current, "event_id", None)
        started = time.perf_counter_ns()
        result = self.scorer.score(claim)
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        if event_id is not None:
            with self._lock:
                self._durations[event_id] = duration_ms
        return result

    def duration_ms(self, event_id: str) -> float | None:
        with self._lock:
            return self._durations.get(event_id)


def validate_schema_name(schema_name: str) -> str:
    """Constrain schema creation/deletion to benchmark-owned names."""

    if not _SCHEMA.fullmatch(schema_name):
        raise ValueError(
            "Benchmark schema must start with claims_bench_ and use safe characters"
        )
    return schema_name


def schema_for_run(run_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", run_id.lower()).strip("_")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    candidate = f"claims_bench_{safe[:40]}_{digest}"
    return validate_schema_name(candidate)


def benchmark_database(database_url: str, schema_name: str) -> PostgresDatabase:
    """Create a disposable schema and return a database scoped to it."""

    import psycopg

    schema_name = validate_schema_name(schema_name)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(schema_name)
            )
        )

    def connect(url: str):
        return psycopg.connect(
            url,
            options=f"-c search_path={schema_name}",
            row_factory=dict_row,
        )

    database = PostgresDatabase(database_url, connect=connect)
    PostgresMigrator(database).upgrade()
    return database


def drop_benchmark_schema(database_url: str, schema_name: str) -> None:
    """Delete only one explicitly validated, benchmark-owned schema."""

    import psycopg

    schema_name = validate_schema_name(schema_name)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema_name)
            )
        )


class DisposableRepositories(AbstractContextManager[PostgresRepositories]):
    """Own one migrated PostgreSQL schema for a benchmark scenario."""

    def __init__(
        self,
        database_url: str,
        run_id: str,
        *,
        keep_schema: bool = False,
    ) -> None:
        self.database_url = database_url
        self.schema_name = schema_for_run(run_id)
        self.keep_schema = keep_schema
        self.repositories: PostgresRepositories | None = None

    def __enter__(self) -> PostgresRepositories:
        database = benchmark_database(self.database_url, self.schema_name)
        self.repositories = PostgresRepositories.from_database(database)
        return self.repositories

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.keep_schema:
            drop_benchmark_schema(self.database_url, self.schema_name)


def repository_manifest(
    *,
    run_id: str,
    benchmark: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Capture non-secret provenance required to interpret one result set."""

    project_root = Path(__file__).resolve().parents[2]

    def command(*arguments: str) -> str:
        try:
            return subprocess.check_output(
                arguments,
                cwd=project_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"

    model_path = (
        project_root
        / "packages/model/artifacts/xgboost-african-motor-v1/model.joblib"
    )
    model_sha256 = (
        hashlib.sha256(model_path.read_bytes()).hexdigest()
        if model_path.exists()
        else "missing"
    )
    git_status = command("git", "status", "--short")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "benchmark": benchmark,
        "created_at": utc_now().isoformat(),
        "git_commit": command("git", "rev-parse", "HEAD"),
        "git_dirty": bool(git_status and git_status != "unavailable"),
        "git_status": git_status.splitlines(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host_logical_cpu_count": os.cpu_count(),
        "host_memory_bytes": command("sysctl", "-n", "hw.memsize"),
        "docker": command("docker", "--version"),
        "docker_cpu_count": command("docker", "info", "--format", "{{.NCPU}}"),
        "docker_memory_bytes": command(
            "docker",
            "info",
            "--format",
            "{{.MemTotal}}",
        ),
        "model_sha256": model_sha256,
        "parameters": parameters,
        "boundaries": {
            "real": [
                "FastAPI routing and validation",
                "canonical claim authorization and hashing",
                "PostgreSQL transactions",
                "Kafka delivery and synchronous offset commits",
                "duplicate and feature processing",
                "XGBoost and local SHAP",
            ],
            "deterministic": [
                "Pinata/IPFS transport",
                "Ethereum RPC and block production",
                "assessment transaction submission",
            ],
        },
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + os.linesep,
        encoding="utf-8",
    )
