"""ERC-2771 preparation and least-privilege relay adapters for Sepolia."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3RPCError

from apps.backend.app.blockchain import ChainSubmission
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentConfigurationError,
    DeploymentValidationError,
    connect_claims_deployment,
    connect_claims_forwarder,
    load_claims_deployment,
)
from packages.integrations.postgres import (
    GaslessSubmissionRecord,
    SignedRelayTransaction,
)

FORWARDER_NAME = "ClaimsRegistryForwarder"
FORWARDER_VERSION = "1"


class GaslessBlockchainError(RuntimeError):
    """Raised when a signed request cannot be safely prepared or relayed."""


def _positive_int(settings: Mapping[str, str], name: str, default: int) -> int:
    """Read a positive policy limit and fail closed on malformed configuration.

    Gas limits and signature lifetimes are security controls, so silently
    accepting zero, a negative value, or a typo would weaken the boundary the
    operator intended to configure.
    """

    raw = settings.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GaslessBlockchainError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise GaslessBlockchainError(f"{name} must be a positive integer")
    return value


def _hex(value: Any) -> str:
    """Normalize Web3 byte-like values to the ``0x`` form stored by the outbox."""

    encoded = value.hex()
    return encoded if encoded.startswith("0x") else f"0x{encoded}"


@dataclass(frozen=True)
class PreparedForwardRequest:
    """The exact request an insurer signs and the forwarder later executes."""

    from_address: str
    to: str
    value: int
    gas: int
    nonce: int
    deadline: int
    data: str

    def contract_value(self, signature: str) -> dict[str, Any]:
        """Build the Solidity tuple accepted by ``ClaimsForwarder``.

        OpenZeppelin's forwarder reads the current nonce from its own storage,
        so the execute tuple does not repeat ``nonce``. Every other field is the
        exact value covered by the insurer's EIP-712 signature.
        """

        return {
            "from": self.from_address,
            "to": self.to,
            "value": self.value,
            "gas": self.gas,
            "deadline": self.deadline,
            "data": self.data,
            "signature": signature,
        }

    def typed_data(self, *, chain_id: int, forwarder_address: str) -> dict[str, Any]:
        """Return the exact JSON-safe EIP-712 document shown to the wallet.

        Domain fields bind the authorization to one chain and one forwarder.
        Integer message fields are strings because JavaScript cannot represent
        every Solidity ``uint256`` without losing precision.
        """

        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "ForwardRequest": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "gas", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint48"},
                    {"name": "data", "type": "bytes"},
                ],
            },
            "primaryType": "ForwardRequest",
            "domain": {
                "name": FORWARDER_NAME,
                "version": FORWARDER_VERSION,
                "chainId": chain_id,
                "verifyingContract": forwarder_address,
            },
            "message": {
                "from": self.from_address,
                "to": self.to,
                "value": str(self.value),
                "gas": str(self.gas),
                "nonce": str(self.nonce),
                "deadline": str(self.deadline),
                "data": self.data,
            },
        }

    @classmethod
    def from_record(cls, record: GaslessSubmissionRecord) -> PreparedForwardRequest:
        """Reconstruct a request from durable fields without inventing defaults.

        A relay retry must sign and execute the same request prepared for the
        insurer. Missing fields therefore indicate corrupt/incomplete outbox
        state and fail before a transaction can be built.
        """

        required = (
            record.call_data,
            record.forwarder_nonce,
            record.forward_gas,
            record.deadline,
        )
        if any(value is None for value in required):
            raise GaslessBlockchainError(
                "Gasless submission does not contain a complete forward request"
            )
        assert record.call_data is not None
        assert record.forwarder_nonce is not None
        assert record.forward_gas is not None
        assert record.deadline is not None
        return cls(
            from_address=Web3.to_checksum_address(record.signer_address),
            to=Web3.to_checksum_address(record.contract_address),
            value=0,
            gas=record.forward_gas,
            nonce=record.forwarder_nonce,
            deadline=record.deadline,
            data=record.call_data,
        )


class GaslessClaimsGateway:
    """Prepare and verify signed claims without receiving a transaction key."""

    def __init__(
        self,
        *,
        rpc_url: str,
        deployment: ClaimsDeployment,
        forward_gas: int,
        signature_ttl_seconds: int,
    ) -> None:
        """Connect to and validate the keyless preparation dependencies.

        Deployment validation checks the configured chain, bytecode, registry
        ABI, forwarder ABI, and trusted-forwarder relationship. The hard caps
        prevent environment settings from expanding sponsorship beyond the
        reviewed policy.
        """

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.deployment = deployment
        try:
            deployment.require_gasless()
            self.registry = connect_claims_deployment(self.w3, deployment)
            self.forwarder = connect_claims_forwarder(self.w3, deployment)
        except (DeploymentConfigurationError, DeploymentValidationError) as exc:
            raise GaslessBlockchainError(str(exc)) from exc
        if forward_gas > 500_000:
            raise GaslessBlockchainError(
                "GASLESS_FORWARD_GAS cannot exceed the 500000 sponsorship cap"
            )
        if signature_ttl_seconds > 3_600:
            raise GaslessBlockchainError(
                "GASLESS_SIGNATURE_TTL_SECONDS cannot exceed 3600"
            )
        self.forward_gas = forward_gas
        self.signature_ttl_seconds = signature_ttl_seconds

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> GaslessClaimsGateway:
        """Construct a gateway from explicit settings for startup and tests."""

        rpc_url = (
            settings.get("SEPOLIA_RPC_URL") or settings.get("RPC_URL") or ""
        ).strip()
        if not rpc_url:
            raise GaslessBlockchainError("SEPOLIA_RPC_URL is required")
        try:
            deployment = load_claims_deployment(settings)
        except DeploymentConfigurationError as exc:
            raise GaslessBlockchainError(str(exc)) from exc
        return cls(
            rpc_url=rpc_url,
            deployment=deployment,
            forward_gas=_positive_int(settings, "GASLESS_FORWARD_GAS", 250_000),
            signature_ttl_seconds=_positive_int(
                settings, "GASLESS_SIGNATURE_TTL_SECONDS", 600
            ),
        )

    @classmethod
    def from_env(cls) -> GaslessClaimsGateway:
        """Construct the keyless gateway from the current process environment."""

        return cls.from_mapping(os.environ)

    def validate_signer(self, signer_address: str) -> str:
        """Verify the authenticated insurer wallet holds the on-chain role."""

        signer = Web3.to_checksum_address(signer_address)
        try:
            if not self.registry.functions.isSubmitter(signer).call():
                raise GaslessBlockchainError(
                    f"Configured insurer signer {signer} does not hold SUBMITTER_ROLE"
                )
        except GaslessBlockchainError:
            raise
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not verify the insurer signer role"
            ) from exc
        return signer

    def prepare_request(
        self,
        *,
        signer_address: str,
        claim_hash: bytes,
        data_pointer: str,
    ) -> PreparedForwardRequest:
        """Create the only forward request the API permits an insurer to sign.

        The server chooses the registry target, zero ETH value, function
        selector, calldata, gas allowance, current forwarder nonce, and short
        deadline. A caller can supply claim content but cannot turn this API
        into a general-purpose transaction sponsor.
        """

        signer = self.validate_signer(signer_address)
        try:
            nonce = int(self.forwarder.functions.nonces(signer).call())
            latest = self.w3.eth.get_block("latest")
            deadline = int(latest["timestamp"]) + self.signature_ttl_seconds
            data = self.registry.encode_abi(
                "submitClaim",
                args=[claim_hash, data_pointer],
            )
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not prepare the insurer's forward request"
            ) from exc
        return PreparedForwardRequest(
            from_address=signer,
            to=self.deployment.address,
            value=0,
            gas=self.forward_gas,
            nonce=nonce,
            deadline=deadline,
            data=data,
        )

    def verify_signature(
        self,
        record: GaslessSubmissionRecord,
        signature: str,
    ) -> None:
        """Ask the deployed forwarder to validate the complete signed request.

        On-chain verification covers the signer, domain, nonce, deadline, and
        request bytes. It runs at authorization time and again before relay so
        a stale request cannot consume sponsored gas after waiting in the queue.
        """

        request = PreparedForwardRequest.from_record(record)
        try:
            valid = self.forwarder.functions.verify(
                request.contract_value(signature)
            ).call()
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not verify the insurer's gasless signature"
            ) from exc
        if not valid:
            raise GaslessBlockchainError(
                "The gasless signature is invalid, expired, or uses a stale nonce"
            )


class GaslessRelayChain(GaslessClaimsGateway):
    """Sign and broadcast only pre-authorized ClaimsForwarder executions."""

    def __init__(
        self,
        *,
        rpc_url: str,
        deployment: ClaimsDeployment,
        private_key: str,
        forward_gas: int,
        signature_ttl_seconds: int,
        max_transaction_gas: int,
        max_fee_per_gas_wei: int,
        max_priority_fee_per_gas_wei: int,
    ) -> None:
        """Create the private-key boundary used only by the relay worker.

        In addition to the preparation checks inherited from the gateway, this
        constructor loads the dedicated payer account, applies fee/gas caps,
        and proves that the payer has no registry business role.
        """

        super().__init__(
            rpc_url=rpc_url,
            deployment=deployment,
            forward_gas=forward_gas,
            signature_ttl_seconds=signature_ttl_seconds,
        )
        try:
            self.account = self.w3.eth.account.from_key(private_key)
        except Exception as exc:
            raise GaslessBlockchainError(
                "SEPOLIA_RELAYER_PRIVATE_KEY is not a valid Ethereum private key"
            ) from exc
        self.max_transaction_gas = max_transaction_gas
        self.max_fee_per_gas_wei = max_fee_per_gas_wei
        self.max_priority_fee_per_gas_wei = max_priority_fee_per_gas_wei
        self._verify_relayer_is_unprivileged()

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> GaslessRelayChain:
        """Build the relay adapter and enforce exactly one private-key source.

        Development may use an environment value for convenience. Production
        requires a mounted file so the key need not appear in the service
        definition or inherited process environment. Human-friendly gwei caps
        are converted to wei once at startup.
        """

        rpc_url = (
            settings.get("SEPOLIA_RPC_URL") or settings.get("RPC_URL") or ""
        ).strip()
        if not rpc_url:
            raise GaslessBlockchainError("SEPOLIA_RPC_URL is required")
        try:
            deployment = load_claims_deployment(settings)
        except DeploymentConfigurationError as exc:
            raise GaslessBlockchainError(str(exc)) from exc
        forward_gas = _positive_int(settings, "GASLESS_FORWARD_GAS", 250_000)
        signature_ttl_seconds = _positive_int(
            settings, "GASLESS_SIGNATURE_TTL_SECONDS", 600
        )
        private_key = settings.get("SEPOLIA_RELAYER_PRIVATE_KEY", "").strip()
        private_key_file = settings.get("SEPOLIA_RELAYER_PRIVATE_KEY_FILE", "").strip()
        if private_key and private_key_file:
            raise GaslessBlockchainError(
                "Configure only one relayer private-key source"
            )
        if private_key_file:
            try:
                private_key = Path(private_key_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise GaslessBlockchainError(
                    "Could not read SEPOLIA_RELAYER_PRIVATE_KEY_FILE"
                ) from exc
        environment = (
            settings.get("DEPLOYMENT_ENVIRONMENT", "development").strip().lower()
        )
        if environment == "production" and not private_key_file:
            raise GaslessBlockchainError(
                "Production relayers must use SEPOLIA_RELAYER_PRIVATE_KEY_FILE"
            )
        if not private_key:
            raise GaslessBlockchainError(
                "SEPOLIA_RELAYER_PRIVATE_KEY or "
                "SEPOLIA_RELAYER_PRIVATE_KEY_FILE is required"
            )
        return cls(
            rpc_url=rpc_url,
            deployment=deployment,
            private_key=private_key,
            forward_gas=forward_gas,
            signature_ttl_seconds=signature_ttl_seconds,
            max_transaction_gas=_positive_int(
                settings, "GASLESS_MAX_TRANSACTION_GAS", 500_000
            ),
            max_fee_per_gas_wei=Web3.to_wei(
                _positive_int(settings, "GASLESS_MAX_FEE_GWEI", 100), "gwei"
            ),
            max_priority_fee_per_gas_wei=Web3.to_wei(
                _positive_int(settings, "GASLESS_MAX_PRIORITY_FEE_GWEI", 3),
                "gwei",
            ),
        )

    @classmethod
    def from_env(cls) -> GaslessRelayChain:
        """Construct the restricted relay adapter from process configuration."""

        return cls.from_mapping(os.environ)

    def _verify_relayer_is_unprivileged(self) -> None:
        """Refuse startup when the gas-paying account also holds a registry role.

        Separating the payer from admin, submitter, and assessor accounts limits
        a relayer-key compromise to the sponsored-call and fee policies enforced
        by this worker.
        """

        address = self.account.address
        try:
            privileged = (
                Web3.to_checksum_address(self.registry.functions.defaultAdmin().call())
                == address
                or self.registry.functions.isSubmitter(address).call()
                or self.registry.functions.isAssessor(address).call()
            )
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not verify the relayer's least-privilege role separation"
            ) from exc
        if privileged:
            raise GaslessBlockchainError(
                "The relayer account must not be an admin, submitter, or assessor"
            )

    def pending_nonce(self) -> int:
        """Read the node's pending nonce as a lower bound for DB allocation.

        PostgreSQL also remembers its next reserved nonce. The repository uses
        the greater value so restarts neither reuse a locally reserved nonce nor
        ignore transactions already visible to the RPC node.
        """

        try:
            return int(
                self.w3.eth.get_transaction_count(self.account.address, "pending")
            )
        except Exception as exc:
            raise GaslessBlockchainError("Could not read the relayer nonce") from exc

    def _fee_quote(
        self,
        *,
        minimum_max_fee_per_gas: int = 0,
        minimum_priority_fee_per_gas: int = 0,
    ) -> tuple[int, int]:
        """Quote capped EIP-1559 fees, including a valid replacement increase.

        A replacement must exceed both previous fee fields; the 12.5% bump is
        rounded upward. If base fees or the required bump exceed sponsorship
        policy, the worker waits instead of spending an unbounded amount.
        """

        try:
            latest = self.w3.eth.get_block("latest")
            base_fee = int(latest.get("baseFeePerGas") or self.w3.eth.gas_price)
            try:
                suggested_priority = int(self.w3.eth.max_priority_fee)
            except (TypeError, ValueError, Web3RPCError):
                suggested_priority = Web3.to_wei(2, "gwei")
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not quote relay transaction fees"
            ) from exc
        quoted_priority = min(
            suggested_priority,
            self.max_priority_fee_per_gas_wei,
        )
        replacement_priority = (
            (minimum_priority_fee_per_gas * 1125 + 999) // 1000
            if minimum_priority_fee_per_gas
            else 0
        )
        priority = max(quoted_priority, replacement_priority)
        replacement_max_fee = (
            (minimum_max_fee_per_gas * 1125 + 999) // 1000
            if minimum_max_fee_per_gas
            else 0
        )
        required_max_fee = max(base_fee + priority, replacement_max_fee)
        if (
            priority > self.max_priority_fee_per_gas_wei
            or required_max_fee > self.max_fee_per_gas_wei
        ):
            raise GaslessBlockchainError(
                "Current network fees exceed the configured sponsorship cap"
            )
        quoted_max_fee = min(
            (base_fee * 2) + priority,
            self.max_fee_per_gas_wei,
        )
        return max(quoted_max_fee, required_max_fee), priority

    def prepare_relay_signer(
        self,
        record: GaslessSubmissionRecord,
        *,
        minimum_max_fee_per_gas: int = 0,
        minimum_priority_fee_per_gas: int = 0,
    ) -> Callable[[int], SignedRelayTransaction]:
        """Validate and estimate remotely, then return a local-only nonce signer.

        RPC calls happen before the repository acquires its nonce-allocation
        advisory lock. The returned closure performs deterministic transaction
        construction and signing only, keeping the critical database section
        short while allowing raw bytes to be persisted before broadcast.
        """

        if not record.insurer_signature:
            raise GaslessBlockchainError(
                "Authorized submission has no insurer signature"
            )
        request = PreparedForwardRequest.from_record(record)
        if (
            record.contract_address.lower() != self.deployment.address.lower()
            or record.forwarder_address.lower()
            != (self.deployment.forwarder_address or "").lower()
            or record.chain_id != self.deployment.chain_id
        ):
            raise GaslessBlockchainError(
                "Submission target does not match the active gasless deployment"
            )
        self.verify_signature(record, record.insurer_signature)
        max_fee, priority_fee = self._fee_quote(
            minimum_max_fee_per_gas=minimum_max_fee_per_gas,
            minimum_priority_fee_per_gas=minimum_priority_fee_per_gas,
        )
        try:
            execute = self.forwarder.functions.execute(
                request.contract_value(record.insurer_signature)
            )
            estimate = int(
                execute.estimate_gas({"from": self.account.address, "value": 0})
            )
            transaction_gas = (estimate * 120 + 99) // 100
            if transaction_gas > self.max_transaction_gas:
                raise GaslessBlockchainError(
                    "Relay transaction exceeds the configured gas cap"
                )
        except GaslessBlockchainError:
            raise
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not construct the allowlisted relay transaction"
            ) from exc

        def sign(relayer_nonce: int) -> SignedRelayTransaction:
            """Sign the prevalidated call for the nonce reserved by PostgreSQL."""

            try:
                transaction = execute.build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": relayer_nonce,
                        "chainId": self.deployment.chain_id,
                        "value": 0,
                        "gas": transaction_gas,
                        "maxFeePerGas": max_fee,
                        "maxPriorityFeePerGas": priority_fee,
                    }
                )
                signed = self.account.sign_transaction(transaction)
            except Exception as exc:
                raise GaslessBlockchainError(
                    "Could not sign the allowlisted relay transaction"
                ) from exc
            return SignedRelayTransaction(
                nonce=relayer_nonce,
                raw_transaction=_hex(signed.raw_transaction),
                transaction_hash=_hex(signed.hash),
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=priority_fee,
            )

        return sign

    def sign_relay(
        self,
        record: GaslessSubmissionRecord,
        *,
        relayer_nonce: int,
        minimum_max_fee_per_gas: int = 0,
        minimum_priority_fee_per_gas: int = 0,
    ) -> SignedRelayTransaction:
        """Convenience wrapper used by diagnostics and focused adapter tests."""

        return self.prepare_relay_signer(
            record,
            minimum_max_fee_per_gas=minimum_max_fee_per_gas,
            minimum_priority_fee_per_gas=minimum_priority_fee_per_gas,
        )(relayer_nonce)

    def broadcast(self, raw_transaction: str, expected_hash: str) -> str:
        """Broadcast persisted bytes idempotently and verify the returned hash.

        ``already known`` is a successful replay. ``nonce too low`` is accepted
        only when the expected transaction has a receipt; otherwise an unknown
        transaction consumed a nonce owned by this outbox and intervention is
        safer than guessing.
        """

        try:
            returned = _hex(self.w3.eth.send_raw_transaction(HexBytes(raw_transaction)))
        except (Web3RPCError, ValueError) as exc:
            message = str(exc).lower()
            if "already known" in message:
                return expected_hash
            if "nonce too low" in message:
                # A crash/retry is safe only when this exact hash actually
                # mined. Otherwise a transaction created outside this outbox
                # consumed the dedicated relayer nonce and needs intervention.
                if self.receipt(expected_hash) is not None:
                    return expected_hash
                raise GaslessBlockchainError(
                    "Relayer nonce was consumed by an unknown transaction"
                ) from exc
            raise GaslessBlockchainError(
                "Ethereum RPC rejected the signed relay transaction"
            ) from exc
        if returned.lower() != expected_hash.lower():
            raise GaslessBlockchainError(
                "Ethereum RPC returned a different relay transaction hash"
            )
        return returned

    def receipt(self, transaction_hash: str) -> Any | None:
        """Return a receipt when mined, while treating not-found as normal pending."""

        try:
            return self.w3.eth.get_transaction_receipt(transaction_hash)
        except TransactionNotFound:
            return None
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not read the relay transaction receipt"
            ) from exc

    def has_confirmations(self, receipt: Any, confirmations: int) -> bool:
        """Return whether the receipt is at or below the configured safe head.

        ``confirmations`` is the number of additional blocks required after the
        receipt block. A value of zero therefore accepts a freshly mined block.
        """

        try:
            latest = int(self.w3.eth.block_number)
            required_head = int(receipt["blockNumber"]) + confirmations
            return latest >= required_head
        except Exception as exc:
            raise GaslessBlockchainError(
                "Could not determine relay transaction confirmation depth"
            ) from exc

    def confirm(
        self,
        record: GaslessSubmissionRecord,
        receipt: Any,
    ) -> ChainSubmission:
        """Convert a successful, semantically matching receipt into a result.

        Transaction success alone is insufficient: exactly one
        ``ClaimSubmitted`` event must name the authorized insurer, claim hash,
        and IPFS pointer. This prevents unrelated or malformed receipts from
        advancing the durable record to ``confirmed``.
        """

        if receipt["status"] != 1:
            raise GaslessBlockchainError("Relay transaction reverted on-chain")
        events = self.registry.events.ClaimSubmitted().process_receipt(receipt)
        if len(events) != 1:
            raise GaslessBlockchainError(
                "Relay transaction did not emit exactly one ClaimSubmitted event"
            )
        event = events[0]["args"]
        if (
            event["claimant"].lower() != record.signer_address.lower()
            or _hex(event["claimHash"]).lower() != (record.claim_hash or "").lower()
            or event["dataPointer"] != record.data_pointer
        ):
            raise GaslessBlockchainError(
                "ClaimSubmitted event does not match the authorized submission"
            )
        return ChainSubmission(
            claim_id=int(event["claimId"]),
            transaction_hash=_hex(receipt["transactionHash"]),
            block_number=int(receipt["blockNumber"]),
        )
