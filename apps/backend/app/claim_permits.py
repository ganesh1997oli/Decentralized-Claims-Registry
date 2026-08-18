"""Insurer-scoped EIP-712 permits for public claim submissions."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

CLAIM_PERMIT_DOMAIN_NAME = "ClaimsRegistry"
CLAIM_PERMIT_DOMAIN_VERSION = "2"
CLAIM_PERMIT_FIELDS = (
    {"name": "claimant", "type": "address"},
    {"name": "submitter", "type": "address"},
    {"name": "insurer", "type": "address"},
    {"name": "claimantCommitment", "type": "bytes32"},
    {"name": "claimHash", "type": "bytes32"},
    {"name": "dataPointerHash", "type": "bytes32"},
    {"name": "permitId", "type": "bytes32"},
    {"name": "deadline", "type": "uint48"},
)


class ClaimPermitConfigurationError(ValueError):
    """Raised when permit-issuer configuration cannot be trusted."""


class ClaimPermitIssuanceError(RuntimeError):
    """Raised when an eligible claim cannot receive an insurer permit."""


@dataclass(frozen=True)
class ClaimPermit:
    """Fixed-width permit fields shared by Python, wallets, and Solidity."""

    claimant: str
    submitter: str
    insurer: str
    claimant_commitment: str
    claim_hash: str
    data_pointer_hash: str
    permit_id: str
    deadline: int

    def message(self) -> dict[str, str | int]:
        """Return the exact camel-case EIP-712 message expected by Solidity."""

        return {
            "claimant": self.claimant,
            "submitter": self.submitter,
            "insurer": self.insurer,
            "claimantCommitment": self.claimant_commitment,
            "claimHash": self.claim_hash,
            "dataPointerHash": self.data_pointer_hash,
            "permitId": self.permit_id,
            "deadline": self.deadline,
        }


@dataclass(frozen=True)
class SignedClaimPermit:
    """Permit plus the scoped issuer recovered by the registry."""

    permit: ClaimPermit
    issuer_address: str
    signature: str


class ClaimPermitIssuer(Protocol):
    """Signing interface used after policy eligibility has been established."""

    def issuer_address_for(self, insurer_id: str) -> str:
        """Return the public signer expected to hold the scoped contract role."""

        ...

    def issue(self, insurer_id: str, permit: ClaimPermit) -> SignedClaimPermit:
        """Sign one exact, short-lived permit for the selected insurer."""

        ...


class FileClaimPermitIssuer:
    """Least-privilege permit signer backed by owner-only key files.

    The signer never pays gas and must hold only an insurer-scoped
    `PERMIT_ISSUER_ROLE`. Production deployments can replace this adapter with a
    managed signer while retaining the same narrow `issue` interface.
    """

    def __init__(
        self,
        accounts: Mapping[str, LocalAccount],
        *,
        chain_id: int,
        registry_address: str,
    ) -> None:
        if not accounts:
            raise ClaimPermitConfigurationError(
                "At least one claim permit issuer is required"
            )
        if chain_id < 1:
            raise ClaimPermitConfigurationError("Permit chain ID must be positive")
        try:
            registry = Web3.to_checksum_address(registry_address)
        except ValueError as exc:
            raise ClaimPermitConfigurationError(
                "Permit registry address is invalid"
            ) from exc
        if int(registry, 16) == 0:
            raise ClaimPermitConfigurationError(
                "Permit registry address cannot be zero"
            )
        normalized: dict[str, LocalAccount] = {}
        issuer_addresses: set[str] = set()
        for insurer_id, account in accounts.items():
            key = insurer_id.strip()
            if not key:
                raise ClaimPermitConfigurationError("Permit insurer ID cannot be empty")
            if account.address.lower() in issuer_addresses:
                raise ClaimPermitConfigurationError(
                    "One permit issuer key cannot be shared by multiple insurers"
                )
            normalized[key] = account
            issuer_addresses.add(account.address.lower())
        self.accounts = normalized
        self.chain_id = chain_id
        self.registry_address = registry

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
        *,
        chain_id: int,
        registry_address: str,
    ) -> FileClaimPermitIssuer:
        raw_json = settings.get("CLAIM_PERMIT_ISSUERS_JSON", "")
        try:
            values = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ClaimPermitConfigurationError(
                "CLAIM_PERMIT_ISSUERS_JSON must be valid JSON"
            ) from exc
        if not isinstance(values, list) or not values:
            raise ClaimPermitConfigurationError(
                "CLAIM_PERMIT_ISSUERS_JSON must contain issuer records"
            )

        accounts: dict[str, LocalAccount] = {}
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                raise ClaimPermitConfigurationError(
                    f"Permit issuer {index} must be an object"
                )
            insurer_id = str(raw.get("insurerId", "")).strip()
            raw_path = str(raw.get("privateKeyFile", "")).strip()
            path = Path(raw_path)
            if not insurer_id or not path.is_absolute():
                raise ClaimPermitConfigurationError(
                    f"Permit issuer {index} requires an insurerId and absolute privateKeyFile"
                )
            try:
                file_stat = path.stat()
                if file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise ClaimPermitConfigurationError(
                        f"Permit key file for {insurer_id} must be owner-only"
                    )
                raw_key = path.read_text(encoding="utf-8").strip()
                if len(raw_key) > 68:
                    raise ValueError("private key file is unexpectedly large")
                account = Account.from_key(raw_key)
            except ClaimPermitConfigurationError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                raise ClaimPermitConfigurationError(
                    f"Could not load permit key file for {insurer_id}"
                ) from exc
            if insurer_id in accounts:
                raise ClaimPermitConfigurationError(
                    f"Duplicate permit issuer insurerId {insurer_id!r}"
                )
            accounts[insurer_id] = account
        return cls(
            accounts,
            chain_id=chain_id,
            registry_address=registry_address,
        )

    @classmethod
    def from_env(
        cls,
        *,
        chain_id: int,
        registry_address: str,
    ) -> FileClaimPermitIssuer:
        return cls.from_mapping(
            os.environ,
            chain_id=chain_id,
            registry_address=registry_address,
        )

    def issuer_address_for(self, insurer_id: str) -> str:
        """Return the public signer address used for readiness role checks."""

        account = self.accounts.get(insurer_id)
        if account is None:
            raise ClaimPermitIssuanceError(
                "No permit issuer is configured for the verified insurer"
            )
        return Web3.to_checksum_address(account.address)

    def issue(self, insurer_id: str, permit: ClaimPermit) -> SignedClaimPermit:
        """Sign an exact permit under the registry's versioned EIP-712 domain."""

        account = self.accounts.get(insurer_id)
        if account is None:
            raise ClaimPermitIssuanceError(
                "No permit issuer is configured for the verified insurer"
            )
        if permit.insurer == Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000000"
        ):
            raise ClaimPermitIssuanceError("A permit insurer cannot be zero")
        try:
            signed = account.sign_typed_data(
                domain_data={
                    "name": CLAIM_PERMIT_DOMAIN_NAME,
                    "version": CLAIM_PERMIT_DOMAIN_VERSION,
                    "chainId": self.chain_id,
                    "verifyingContract": self.registry_address,
                },
                message_types={"ClaimPermit": list(CLAIM_PERMIT_FIELDS)},
                message_data=permit.message(),
            )
        except (ValueError, TypeError) as exc:
            raise ClaimPermitIssuanceError(
                "The verified claim permit could not be signed"
            ) from exc
        encoded_signature = signed.signature.hex()
        if not encoded_signature.startswith("0x"):
            encoded_signature = f"0x{encoded_signature}"
        return SignedClaimPermit(
            permit=permit,
            issuer_address=Web3.to_checksum_address(account.address),
            signature=encoded_signature,
        )
