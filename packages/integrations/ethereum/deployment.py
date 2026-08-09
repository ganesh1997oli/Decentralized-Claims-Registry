"""Select and validate the ClaimsRegistry deployment used by every process.

The deployment ID is the only runtime selector.  Module IDs, artifact paths,
addresses, and ABI compatibility stay behind this module so the API, listener,
worker, and command-line demo cannot silently choose different contracts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3

SEPOLIA_CHAIN_ID = 11_155_111
CLAIMS_REGISTRY_MODULE_ID = "ClaimsRegistryModule#ClaimsRegistry"
CLAIMS_FORWARDER_MODULE_ID = "ClaimsRegistryModule#ClaimsForwarder"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEPLOYMENTS_ROOT = (
    PROJECT_ROOT / "apps" / "contracts" / "ignition" / "deployments"
)

REQUIRED_FUNCTIONS = frozenset(
    {
        "assessClaim",
        "claimCount",
        "defaultAdmin",
        "getClaim",
        "isAssessor",
        "isSubmitter",
        "submitClaim",
        "verifyClaimData",
    }
)
REQUIRED_EVENTS = frozenset({"ClaimAssessed", "ClaimSubmitted"})
GASLESS_REGISTRY_FUNCTIONS = frozenset({"isAssessorFor", "trustedForwarder"})
FORWARDER_REQUIRED_FUNCTIONS = frozenset(
    {"eip712Domain", "execute", "nonces", "verify"}
)
_SAFE_DEPLOYMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class DeploymentConfigurationError(ValueError):
    """Raised when a selected local deployment is missing or incompatible."""


class DeploymentValidationError(RuntimeError):
    """Raised when the selected artifact does not match the live RPC contract."""


@dataclass(frozen=True)
class ClaimsDeployment:
    """The validated address and contract interface for one Sepolia deployment."""

    deployment_id: str
    chain_id: int
    address: str
    abi: tuple[dict[str, Any], ...]
    forwarder_address: str | None = None
    forwarder_abi: tuple[dict[str, Any], ...] = ()

    @property
    def supports_gasless(self) -> bool:
        """Return whether the selected artifact includes the production relay."""

        return self.forwarder_address is not None and bool(self.forwarder_abi)

    def require_gasless(self) -> None:
        """Fail closed before sponsorship when a legacy deployment is selected."""

        if not self.supports_gasless:
            raise DeploymentConfigurationError(
                f"Deployment {self.deployment_id!r} does not include the "
                "ERC-2771 ClaimsForwarder; deploy and select the gasless module"
            )


def load_claims_deployment(
    settings: Mapping[str, str],
    *,
    deployments_root: Path = DEFAULT_DEPLOYMENTS_ROOT,
) -> ClaimsDeployment:
    """Load the explicitly selected, hardened ClaimsRegistry artifact."""

    deployment_id = settings.get("CLAIMS_DEPLOYMENT_ID", "").strip()
    if not deployment_id:
        raise DeploymentConfigurationError(
            "CLAIMS_DEPLOYMENT_ID is required; use 'sepolia-gasless-v1' for "
            "sponsored writes or 'sepolia-security-audit-v1' for legacy reads"
        )
    if not _SAFE_DEPLOYMENT_ID.fullmatch(deployment_id) or ".." in deployment_id:
        raise DeploymentConfigurationError(
            "CLAIMS_DEPLOYMENT_ID must be a single safe deployment directory name"
        )

    deployments_root = deployments_root.resolve()
    deployment_dir = (deployments_root / deployment_id).resolve()
    if deployments_root not in deployment_dir.parents:
        raise DeploymentConfigurationError(
            "CLAIMS_DEPLOYMENT_ID resolves outside the deployments directory"
        )
    address_path = deployment_dir / "deployed_addresses.json"
    artifact_path = deployment_dir / "artifacts" / f"{CLAIMS_REGISTRY_MODULE_ID}.json"
    try:
        addresses = json.loads(address_path.read_text(encoding="utf-8"))
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        address = Web3.to_checksum_address(addresses[CLAIMS_REGISTRY_MODULE_ID])
        raw_abi = artifact["abi"]
        if not isinstance(raw_abi, list) or not all(
            isinstance(entry, dict) for entry in raw_abi
        ):
            raise TypeError("artifact ABI must be a list of objects")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentConfigurationError(
            f"Could not load ClaimsRegistry deployment {deployment_id!r} "
            f"from {deployment_dir}"
        ) from exc

    functions = {
        str(entry.get("name")) for entry in raw_abi if entry.get("type") == "function"
    }
    events = {
        str(entry.get("name")) for entry in raw_abi if entry.get("type") == "event"
    }
    missing_functions = sorted(REQUIRED_FUNCTIONS - functions)
    missing_events = sorted(REQUIRED_EVENTS - events)
    if missing_functions or missing_events:
        missing = ", ".join(missing_functions + missing_events)
        raise DeploymentConfigurationError(
            f"Deployment {deployment_id!r} uses an incompatible ClaimsRegistry "
            f"interface; missing: {missing}"
        )

    # The security-audit deployment predates gasless submission and remains
    # readable for migration/history. A production writer requires both module
    # artifacts and the stricter v2 registry interface below.
    forwarder_address: str | None = None
    forwarder_abi: tuple[dict[str, Any], ...] = ()
    if CLAIMS_FORWARDER_MODULE_ID in addresses:
        forwarder_artifact_path = (
            deployment_dir / "artifacts" / f"{CLAIMS_FORWARDER_MODULE_ID}.json"
        )
        try:
            forwarder_address = Web3.to_checksum_address(
                addresses[CLAIMS_FORWARDER_MODULE_ID]
            )
            forwarder_artifact = json.loads(
                forwarder_artifact_path.read_text(encoding="utf-8")
            )
            raw_forwarder_abi = forwarder_artifact["abi"]
            if not isinstance(raw_forwarder_abi, list) or not all(
                isinstance(entry, dict) for entry in raw_forwarder_abi
            ):
                raise TypeError("forwarder artifact ABI must be a list of objects")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DeploymentConfigurationError(
                f"Could not load ClaimsForwarder deployment {deployment_id!r} "
                f"from {deployment_dir}"
            ) from exc

        forwarder_functions = {
            str(entry.get("name"))
            for entry in raw_forwarder_abi
            if entry.get("type") == "function"
        }
        missing_forwarder = sorted(FORWARDER_REQUIRED_FUNCTIONS - forwarder_functions)
        missing_registry = sorted(GASLESS_REGISTRY_FUNCTIONS - functions)
        if missing_forwarder or missing_registry:
            missing = ", ".join(missing_registry + missing_forwarder)
            raise DeploymentConfigurationError(
                f"Deployment {deployment_id!r} uses an incompatible gasless "
                f"interface; missing: {missing}"
            )
        forwarder_abi = tuple(raw_forwarder_abi)

    return ClaimsDeployment(
        deployment_id=deployment_id,
        chain_id=SEPOLIA_CHAIN_ID,
        address=address,
        abi=tuple(raw_abi),
        forwarder_address=forwarder_address,
        forwarder_abi=forwarder_abi,
    )


def connect_claims_deployment(w3: Any, deployment: ClaimsDeployment) -> Any:
    """Verify network, bytecode, and hardened interface before returning a contract."""

    try:
        connected = w3.is_connected()
    except Exception as exc:
        raise DeploymentValidationError(
            "Could not connect to the configured Ethereum RPC endpoint"
        ) from exc
    if not connected:
        raise DeploymentValidationError(
            "Could not connect to the configured Ethereum RPC endpoint"
        )
    try:
        chain_id = int(w3.eth.chain_id)
    except Exception as exc:
        raise DeploymentValidationError(
            "Could not read the chain ID from the Ethereum RPC endpoint"
        ) from exc
    if chain_id != deployment.chain_id:
        raise DeploymentValidationError(
            f"Deployment {deployment.deployment_id!r} requires chain "
            f"{deployment.chain_id}, but the RPC returned {chain_id}"
        )
    try:
        code = w3.eth.get_code(deployment.address)
    except Exception as exc:
        raise DeploymentValidationError(
            f"Could not read bytecode for {deployment.address}"
        ) from exc
    try:
        code_is_empty = not code or bytes(code) == b""
    except (TypeError, ValueError) as exc:
        raise DeploymentValidationError(
            f"RPC returned invalid bytecode for {deployment.address}"
        ) from exc
    if code_is_empty:
        raise DeploymentValidationError(
            f"No contract bytecode exists at selected address {deployment.address}"
        )

    try:
        contract = w3.eth.contract(
            address=deployment.address,
            abi=deployment.abi,
        )
        contract.functions.defaultAdmin().call()
        if deployment.supports_gasless:
            configured_forwarder = Web3.to_checksum_address(
                contract.functions.trustedForwarder().call()
            )
            if configured_forwarder != deployment.forwarder_address:
                raise DeploymentValidationError(
                    "ClaimsRegistry trusts a different forwarder than its deployment artifact"
                )
    except Exception as exc:
        raise DeploymentValidationError(
            "The selected address does not expose the hardened ClaimsRegistry interface"
        ) from exc
    return contract


def connect_claims_forwarder(w3: Any, deployment: ClaimsDeployment) -> Any:
    """Return the checked-in forwarder only after full deployment validation.

    Registry validation first proves RPC chain, registry bytecode/interface, and
    the registry's trusted-forwarder link. The nonce probe then proves the second
    address exposes the expected forwarder interface before sponsorship starts.
    """

    deployment.require_gasless()
    # Registry validation also proves the RPC chain and the immutable trust link.
    connect_claims_deployment(w3, deployment)
    assert deployment.forwarder_address is not None
    try:
        code = w3.eth.get_code(deployment.forwarder_address)
        if not code or bytes(code) == b"":
            raise DeploymentValidationError(
                f"No contract bytecode exists at forwarder {deployment.forwarder_address}"
            )
        forwarder = w3.eth.contract(
            address=deployment.forwarder_address,
            abi=deployment.forwarder_abi,
        )
        forwarder.functions.nonces("0x0000000000000000000000000000000000000000").call()
    except DeploymentValidationError:
        raise
    except Exception as exc:
        raise DeploymentValidationError(
            "The selected address does not expose the ClaimsForwarder interface"
        ) from exc
    return forwarder
