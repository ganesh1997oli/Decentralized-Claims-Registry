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
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEPLOYMENTS_ROOT = (
    PROJECT_ROOT / "apps" / "contracts" / "ignition" / "deployments"
)

REQUIRED_FUNCTIONS = frozenset(
    {
        "assessClaim",
        "assessorInsurer",
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


def load_claims_deployment(
    settings: Mapping[str, str],
    *,
    deployments_root: Path = DEFAULT_DEPLOYMENTS_ROOT,
) -> ClaimsDeployment:
    """Load the explicitly selected, hardened ClaimsRegistry artifact."""

    deployment_id = settings.get("CLAIMS_DEPLOYMENT_ID", "").strip()
    if not deployment_id:
        raise DeploymentConfigurationError(
            "CLAIMS_DEPLOYMENT_ID is required; use "
            "'sepolia-security-audit-v1' for the checked-in hardened deployment"
        )
    if (
        not _SAFE_DEPLOYMENT_ID.fullmatch(deployment_id)
        or ".." in deployment_id
    ):
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
    artifact_path = (
        deployment_dir / "artifacts" / f"{CLAIMS_REGISTRY_MODULE_ID}.json"
    )
    try:
        addresses = json.loads(address_path.read_text(encoding="utf-8"))
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        address = Web3.to_checksum_address(
            addresses[CLAIMS_REGISTRY_MODULE_ID]
        )
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
        str(entry.get("name"))
        for entry in raw_abi
        if entry.get("type") == "function"
    }
    events = {
        str(entry.get("name"))
        for entry in raw_abi
        if entry.get("type") == "event"
    }
    missing_functions = sorted(REQUIRED_FUNCTIONS - functions)
    missing_events = sorted(REQUIRED_EVENTS - events)
    if missing_functions or missing_events:
        missing = ", ".join(missing_functions + missing_events)
        raise DeploymentConfigurationError(
            f"Deployment {deployment_id!r} uses an incompatible ClaimsRegistry "
            f"interface; missing: {missing}"
        )

    return ClaimsDeployment(
        deployment_id=deployment_id,
        chain_id=SEPOLIA_CHAIN_ID,
        address=address,
        abi=tuple(raw_abi),
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
    except Exception as exc:
        raise DeploymentValidationError(
            "The selected address does not expose the hardened ClaimsRegistry interface"
        ) from exc
    return contract
