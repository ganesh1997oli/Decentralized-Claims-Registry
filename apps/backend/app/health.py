"""Operational liveness and readiness reporting for the FastAPI process.

Liveness must never depend on a remote system: restarting a healthy process
does not repair PostgreSQL or Sepolia. Readiness is intentionally different. It
checks every dependency required to accept traffic and returns stable messages
that do not expose credentials, connection strings or upstream response bodies.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from apps.backend.app.claimant_auth import ClaimantSessionManager
from apps.backend.app.gasless_blockchain import GaslessClaimsGateway
from apps.backend.app.governance_auth import GovernanceBoundary
from apps.backend.app.indexer_operations import IndexerOperationsBoundary
from apps.backend.app.policy_eligibility import ConfiguredPolicyEligibility
from apps.backend.app.submission_auth import ClaimAuthorizationSigner
from packages.integrations.ethereum import load_claims_deployment
from packages.integrations.ipfs import IPFSClient
from packages.integrations.postgres import PostgresMigrator, PostgresRepositories
from packages.integrations.privacy import ClaimEnvelopeCipher
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

    def check_claimant_authentication() -> None:
        """Validate wallet-session policy against the durable challenge store."""

        repositories = PostgresRepositories.from_env()
        deployment = load_claims_deployment(os.environ)
        ClaimantSessionManager.from_env(
            repositories.claimant_auth_challenges,
            chain_id=deployment.chain_id,
        )

    def check_operations_authentication() -> None:
        """Validate that the digest-only operations boundary can be constructed."""

        IndexerOperationsBoundary.from_env()

    def check_postgres() -> None:
        """Require a reachable database whose schema matches checked-in migrations."""

        repositories = PostgresRepositories.from_env()
        repositories.database.ping()
        PostgresMigrator(repositories.database).require_current()

    def check_sepolia_contract() -> None:
        """Verify contracts, trust link, and every public-intake role binding."""

        # FastAPI is deliberately transaction-keyless. This check validates both
        # contracts plus every insurer/permit-issuer pair without constructing
        # the standalone gas-paying relayer adapter.
        eligibility = ConfiguredPolicyEligibility.from_env()
        gateway = GaslessClaimsGateway.from_env()
        gateway.deployment.require_public_intake()
        for insurer_id, insurer_address in eligibility.configured_insurers:
            gateway.validate_public_intake_configuration(
                insurer_id,
                insurer_address,
            )

    def check_submission_dependencies() -> None:
        """Validate upload and signing configuration without performing a write."""

        # Construction validates the Pinata upload configuration and claim
        # authorization key. Sepolia itself has a dedicated result above.
        IPFSClient.from_env(require_upload=True)
        ClaimAuthorizationSigner.from_env()
        ClaimEnvelopeCipher.from_env()
        if len(os.environ.get("GASLESS_REQUEST_FINGERPRINT_KEY", "")) < 32:
            raise ValueError(
                "GASLESS_REQUEST_FINGERPRINT_KEY must contain at least 32 bytes"
            )

    def check_coverage_governance() -> None:
        """Require proposal authentication and a governance-capable ABI."""

        GovernanceBoundary.from_env()
        load_claims_deployment(os.environ).require_governance()

    return ReadinessProbe(
        (
            ReadinessCheck(
                "claimant_authentication",
                check_claimant_authentication,
                "claimant wallet authentication is unavailable",
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
                "the gasless Sepolia deployment is unavailable",
            ),
            ReadinessCheck(
                "submission_dependencies",
                check_submission_dependencies,
                "claim submission dependencies are unavailable",
            ),
            ReadinessCheck(
                "coverage_governance",
                check_coverage_governance,
                "coverage governance configuration is unavailable",
            ),
        )
    )
