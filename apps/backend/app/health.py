"""Operational liveness and readiness reporting for the FastAPI process.

Liveness must never depend on a remote system: restarting a healthy process
does not repair PostgreSQL or Sepolia. Readiness is intentionally different. It
checks every dependency required to accept traffic and returns stable messages
that do not expose credentials, connection strings or upstream response bodies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from apps.backend.app.blockchain import SepoliaClaimsRegistry
from apps.backend.app.indexer_operations import IndexerOperationsBoundary
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    SubmissionBoundary,
)
from packages.integrations.ipfs import IPFSClient
from packages.integrations.postgres import PostgresMigrator, PostgresRepositories
from packages.observability import get_event_logger

logger = get_event_logger(__name__)


@dataclass(frozen=True)
class ReadinessCheck:
    """One named check and its fixed public failure description."""

    name: str
    run: Callable[[], None]
    failure_message: str


@dataclass(frozen=True)
class ReadinessResult:
    """The complete result returned through the readiness interface."""

    ready: bool
    checks: dict[str, str]


class ReadinessProbe:
    """Evaluate required dependencies without exposing their implementations."""

    def __init__(self, checks: Iterable[ReadinessCheck]) -> None:
        """Freeze a non-empty set of uniquely named checks for stable output.

        Materializing the iterable once prevents a generator from disappearing
        after the first request. Unique names guarantee that the result dictionary
        cannot silently overwrite one dependency with another.
        """

        configured = tuple(checks)
        if not configured:
            raise ValueError("At least one readiness check is required")
        names = [check.name for check in configured]
        if len(names) != len(set(names)):
            raise ValueError("Readiness check names must be unique")
        self._checks = configured

    def evaluate(self) -> ReadinessResult:
        """Execute every check and return a complete, sanitized status map.

        One failure does not short-circuit later checks, which gives an operator a
        useful incident snapshot. Adapter exception text is intentionally omitted
        from both logs and responses because it may contain credential-bearing
        URLs; the exception type remains available for correlation.
        """

        results: dict[str, str] = {}
        ready = True
        for check in self._checks:
            try:
                check.run()
            except Exception as exc:  # noqa: BLE001 - adapters normalize failures
                ready = False
                results[check.name] = check.failure_message
                # Logs keep the exception type for diagnosis but intentionally
                # omit its text, which could contain an upstream URL or secret.
                logger.warning(
                    "readiness.check_failed",
                    check=check.name,
                    exception_type=type(exc).__name__,
                )
            else:
                results[check.name] = "ok"
        return ReadinessResult(ready=ready, checks=results)


def build_readiness_probe() -> ReadinessProbe:
    """Build production checks from environment-backed adapters.

    Each invocation of a check opens fresh lightweight connections. A readiness
    response therefore reflects current dependency availability rather than a
    successful connection cached during process startup.
    """

    def check_insurer_authentication() -> None:
        """Validate insurer API-key configuration without authenticating a request."""

        SubmissionBoundary.from_env()

    def check_operations_authentication() -> None:
        """Validate that the digest-only operations boundary can be constructed."""

        IndexerOperationsBoundary.from_env()

    def check_postgres() -> None:
        """Require a reachable database whose schema matches checked-in migrations."""

        repositories = PostgresRepositories.from_env()
        repositories.database.ping()
        PostgresMigrator(repositories.database).require_current()

    def check_sepolia_contract() -> None:
        """Verify RPC network, deployed bytecode, interface, and submitter role."""

        # The write-capable adapter verifies chain ID, bytecode, hardened
        # interface and SUBMITTER_ROLE without submitting a transaction.
        SepoliaClaimsRegistry.from_env()

    def check_submission_dependencies() -> None:
        """Validate upload and signing configuration without performing a write."""

        # Construction validates the Pinata upload configuration and claim
        # authorization key. Sepolia itself has a dedicated result above.
        IPFSClient.from_env(require_upload=True)
        ClaimAuthorizationSigner.from_env()

    return ReadinessProbe(
        (
            ReadinessCheck(
                "insurer_authentication",
                check_insurer_authentication,
                "insurer authentication configuration is unavailable",
            ),
            ReadinessCheck(
                "indexer_operations_authentication",
                check_operations_authentication,
                "indexer operations authentication configuration is unavailable",
            ),
            ReadinessCheck(
                "postgres",
                check_postgres,
                "PostgreSQL is unavailable or migrations are pending",
            ),
            ReadinessCheck(
                "sepolia_contract",
                check_sepolia_contract,
                "the hardened Sepolia deployment is unavailable",
            ),
            ReadinessCheck(
                "submission_dependencies",
                check_submission_dependencies,
                "claim submission dependencies are unavailable",
            ),
        )
    )
