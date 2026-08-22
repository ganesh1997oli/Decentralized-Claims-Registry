"""Keep all Sepolia-specific behaviour behind one small Python adapter.

The service layer should be able to talk in terms of claims and assessments
without knowing about nonces, ABI files, receipts, or event decoding. This file
owns those details and checks each write rather than treating a submitted
transaction hash as proof that the contract accepted it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from web3 import Web3
from web3.exceptions import Web3RPCError

from packages.integrations.ethereum import (
    SEPOLIA_CHAIN_ID,
    ClaimsDeployment,
    DeploymentConfigurationError,
    DeploymentValidationError,
    connect_claims_deployment,
    load_claims_deployment,
)

logger = logging.getLogger(__name__)


class BlockchainSubmissionError(RuntimeError):
    """Raised when a claim cannot be committed to the registry."""


@dataclass(frozen=True)
class ChainSubmission:
    """Confirmed submission identity decoded from ``ClaimSubmitted``."""

    claim_id: int
    transaction_hash: str
    block_number: int


@dataclass(frozen=True)
class ChainAssessment:
    """Confirmed assessment values decoded from ``ClaimAssessed``."""

    transaction_hash: str
    block_number: int
    status: int
    fraud_score: int


@dataclass(frozen=True)
class ChainClaim:
    """Public Solidity claim state used by reconciliation."""

    claim_id: int
    claimant: str
    claim_hash: str
    data_pointer: str
    status: int
    fraud_score: int
    submitted_at: int
    updated_at: int


def _hex(value: object) -> str:
    """Normalize Web3/HexBytes output to one ``0x``-prefixed representation."""

    encoded = value.hex()  # type: ignore[union-attr]
    return encoded if encoded.startswith("0x") else f"0x{encoded}"


class SepoliaClaimsRegistry:
    """Read the registry and optionally sign writes with a testnet wallet."""

    def __init__(
        self,
        *,
        rpc_url: str,
        private_key: str | None,
        deployment: ClaimsDeployment,
        access: Literal["read", "submitter", "assessor"],
        receipt_timeout: int = 180,
        private_key_env: str = "SEPOLIA_SUBMITTER_PRIVATE_KEY",
    ) -> None:
        """Connect to one verified deployment and configure optional signing.

        ``access`` is an explicit capability: read clients reject private keys,
        while submitter/assessor clients verify the wallet's contract role before
        doing work. One process-local nonce lock serializes writes made through
        this adapter; mined receipts and decoded events remain the final proof.
        """

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.deployment = deployment
        try:
            self.contract = connect_claims_deployment(self.w3, deployment)
        except DeploymentValidationError as exc:
            raise BlockchainSubmissionError(str(exc)) from exc
        logger.info(
            "Selected ClaimsRegistry deployment=%s chain=%s address=%s",
            deployment.deployment_id,
            deployment.chain_id,
            deployment.address,
        )

        # Public reads need only the RPC endpoint and deployed contract address.
        # The query-only FastAPI dependency passes no key, keeping
        # signing authority out of a code path that can never submit a write.
        self.account = None
        self.private_key_env = private_key_env
        if private_key is not None:
            try:
                self.account = self.w3.eth.account.from_key(private_key)
            except Exception as exc:
                raise BlockchainSubmissionError(
                    f"{private_key_env} is not a valid Ethereum private key"
                ) from exc
        self._verify_signer_access(access)
        self.receipt_timeout = receipt_timeout
        # Ethereum accepts each wallet nonce only once. FastAPI and the worker can
        # issue writes close together, so keep nonce allocation inside one lock
        # instead of letting two requests accidentally build the same transaction.
        self._submission_lock = threading.Lock()
        self._next_nonce: int | None = None

    def _verify_signer_access(
        self, access: Literal["read", "submitter", "assessor"]
    ) -> None:
        """Fail before work starts when the client violates its requested role.

        Read access enforces absence of a private key. Write access checks the
        hardened contract's role mappings through RPC rather than trusting
        environment variable naming. Per-insurer assessor scope is enforced by
        the contract for each claim because one assessor may have many scopes.
        """

        if access == "read":
            if self.account is not None:
                raise BlockchainSubmissionError(
                    "Read-only registry clients must not receive a private key"
                )
            return
        if self.account is None:
            raise BlockchainSubmissionError(
                f"{self.private_key_env} is required for {access} access"
            )

        try:
            if access == "submitter":
                authorized = self.contract.functions.isSubmitter(
                    self.account.address
                ).call()
                if not authorized:
                    raise BlockchainSubmissionError(
                        f"{self.account.address} is not an authorized submitter "
                        f"for deployment {self.deployment.deployment_id!r}"
                    )
                return

            authorized = self.contract.functions.isAssessor(self.account.address).call()
            if not authorized:
                raise BlockchainSubmissionError(
                    f"{self.account.address} is not an authorized assessor for deployment "
                    f"{self.deployment.deployment_id!r}"
                )
        except BlockchainSubmissionError:
            raise
        except Exception as exc:
            raise BlockchainSubmissionError(
                f"Could not verify {access} access for {self.account.address}"
            ) from exc

    @classmethod
    def from_env(
        cls,
        *,
        require_private_key: bool = True,
        private_key_env: str = "SEPOLIA_SUBMITTER_PRIVATE_KEY",
    ) -> SepoliaClaimsRegistry:
        """Create either a read-only client or a transaction-capable client.

        Write callers use the safe default and must provide a wallet. Public
        query callers explicitly opt out, and the environment key is not even
        loaded into that client.
        """

        rpc_url = os.environ.get("SEPOLIA_RPC_URL") or os.environ.get("RPC_URL")
        private_key = os.environ.get(private_key_env) if require_private_key else None
        private_key_file_env = f"{private_key_env}_FILE"
        private_key_file = (
            os.environ.get(private_key_file_env, "").strip()
            if require_private_key
            else ""
        )
        if private_key and private_key_file:
            raise BlockchainSubmissionError(
                f"Configure only one of {private_key_env} and {private_key_file_env}"
            )
        if private_key_file:
            try:
                private_key = Path(private_key_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise BlockchainSubmissionError(
                    f"Could not read {private_key_file_env}"
                ) from exc
        if (
            require_private_key
            and os.environ.get("DEPLOYMENT_ENVIRONMENT", "development").strip().lower()
            == "production"
            and not private_key_file
        ):
            raise BlockchainSubmissionError(
                f"Production writers must use {private_key_file_env}"
            )
        missing = [
            name
            for name, value in (
                ("SEPOLIA_RPC_URL", rpc_url),
                (
                    f"{private_key_env} or {private_key_file_env}",
                    private_key if require_private_key else "not-required",
                ),
            )
            if not value
        ]
        if missing:
            raise BlockchainSubmissionError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            )

        try:
            deployment = load_claims_deployment(os.environ)
        except DeploymentConfigurationError as exc:
            raise BlockchainSubmissionError(str(exc)) from exc

        access: Literal["read", "submitter", "assessor"]
        if not require_private_key:
            access = "read"
        elif private_key_env == "SEPOLIA_SUBMITTER_PRIVATE_KEY":
            access = "submitter"
        elif private_key_env == "SEPOLIA_ASSESSOR_PRIVATE_KEY":
            access = "assessor"
        else:
            raise BlockchainSubmissionError(
                f"Unsupported blockchain signer setting: {private_key_env}"
            )

        return cls(
            rpc_url=rpc_url,
            private_key=private_key,
            deployment=deployment,
            access=access,
            receipt_timeout=int(os.environ.get("RECEIPT_TIMEOUT", "180")),
            private_key_env=private_key_env,
        )

    def _signing_account(self):
        """Return the configured signer or fail before constructing a write.

        This guard keeps internal write methods safe even if a read-only adapter is
        passed through an incorrectly wired dependency.
        """

        if self.account is None:
            raise BlockchainSubmissionError(
                f"{self.private_key_env} is required for blockchain writes"
            )
        return self.account

    def submit_claim(self, claim_hash: bytes, data_pointer: str) -> ChainSubmission:
        """Submit a claim and derive its ID from the mined contract event.

        Nonces are allocated under a lock and one explicit higher-nonce response
        may repair a stale public-RPC view. Receipt status proves execution; exactly
        one decoded ``ClaimSubmitted`` event proves which ID the contract assigned.
        Any uncertain failure clears the cached nonce before the next attempt.
        """

        try:
            account = self._signing_account()
            with self._submission_lock:
                if self._next_nonce is None:
                    self._next_nonce = self.w3.eth.get_transaction_count(
                        account.address, "pending"
                    )

                for attempt in range(2):
                    nonce = self._next_nonce
                    transaction = self.contract.functions.submitClaim(
                        claim_hash, data_pointer
                    ).build_transaction(
                        {
                            "from": account.address,
                            "nonce": nonce,
                            "chainId": SEPOLIA_CHAIN_ID,
                        }
                    )
                    signed = account.sign_transaction(transaction)
                    try:
                        transaction_hash = self.w3.eth.send_raw_transaction(
                            signed.raw_transaction
                        )
                    except Web3RPCError as exc:
                        # A public RPC can briefly report an old nonce. If it tells
                        # us the newer one, retry once instead of failing the claim.
                        match = re.search(
                            r"next nonce\s+(\d+)", str(exc), re.IGNORECASE
                        )
                        if attempt == 0 and match and int(match.group(1)) > nonce:
                            self._next_nonce = int(match.group(1))
                            continue
                        self._next_nonce = None
                        raise

                    self._next_nonce = nonce + 1
                    break

                receipt = self.w3.eth.wait_for_transaction_receipt(
                    transaction_hash, timeout=self.receipt_timeout
                )

            if receipt["status"] != 1:
                raise BlockchainSubmissionError(
                    f"Sepolia transaction reverted: {_hex(transaction_hash)}"
                )

            # A successful receipt says the transaction ran, while the event tells
            # us which claim ID the contract actually assigned to this submission.
            events = self.contract.events.ClaimSubmitted().process_receipt(receipt)
            if len(events) != 1:
                raise BlockchainSubmissionError(
                    "Transaction did not emit exactly one ClaimSubmitted event"
                )

            return ChainSubmission(
                claim_id=events[0]["args"]["claimId"],
                transaction_hash=_hex(transaction_hash),
                block_number=receipt["blockNumber"],
            )
        except BlockchainSubmissionError:
            raise
        except Exception as exc:
            self._next_nonce = None
            raise BlockchainSubmissionError(
                f"Sepolia submission failed: {exc}"
            ) from exc

    def assess_claim(
        self, claim_id: int, status: int, fraud_score: int
    ) -> ChainAssessment:
        """Write a bounded model result and verify the emitted assessment values.

        Status and basis-point score are checked before transaction construction.
        After mining, the decoded event must match claim ID, status, and score; a
        successful transaction with unexpected event data is still rejected.
        """

        if status not in range(5):
            raise BlockchainSubmissionError(f"Invalid claim status: {status}")
        if fraud_score not in range(10_001):
            raise BlockchainSubmissionError(
                f"Fraud score must be between 0 and 10000, got {fraud_score}"
            )

        try:
            account = self._signing_account()
            with self._submission_lock:
                if self._next_nonce is None:
                    self._next_nonce = self.w3.eth.get_transaction_count(
                        account.address, "pending"
                    )

                for attempt in range(2):
                    nonce = self._next_nonce
                    transaction = self.contract.functions.assessClaim(
                        claim_id, status, fraud_score
                    ).build_transaction(
                        {
                            "from": account.address,
                            "nonce": nonce,
                            "chainId": SEPOLIA_CHAIN_ID,
                        }
                    )
                    signed = account.sign_transaction(transaction)
                    try:
                        transaction_hash = self.w3.eth.send_raw_transaction(
                            signed.raw_transaction
                        )
                    except Web3RPCError as exc:
                        # Use the same one-time stale nonce recovery as submission.
                        match = re.search(
                            r"next nonce\s+(\d+)", str(exc), re.IGNORECASE
                        )
                        if attempt == 0 and match and int(match.group(1)) > nonce:
                            self._next_nonce = int(match.group(1))
                            continue
                        self._next_nonce = None
                        raise

                    self._next_nonce = nonce + 1
                    break

                receipt = self.w3.eth.wait_for_transaction_receipt(
                    transaction_hash, timeout=self.receipt_timeout
                )

            if receipt["status"] != 1:
                raise BlockchainSubmissionError(
                    f"Sepolia assessment reverted: {_hex(transaction_hash)}"
                )

            events = self.contract.events.ClaimAssessed().process_receipt(receipt)
            if len(events) != 1:
                raise BlockchainSubmissionError(
                    "Transaction did not emit exactly one ClaimAssessed event"
                )
            # Do not trust only the transaction status. Check that the event data
            # contains the claim, status, and score we asked the contract to save.
            event = events[0]["args"]
            if (
                event["claimId"] != claim_id
                or event["newStatus"] != status
                or event["fraudScore"] != fraud_score
            ):
                raise BlockchainSubmissionError(
                    "ClaimAssessed event did not match the requested assessment"
                )

            return ChainAssessment(
                transaction_hash=_hex(transaction_hash),
                block_number=receipt["blockNumber"],
                status=status,
                fraud_score=fraud_score,
            )
        except BlockchainSubmissionError:
            raise
        except Exception as exc:
            self._next_nonce = None
            raise BlockchainSubmissionError(
                f"Sepolia assessment failed: {exc}"
            ) from exc

    def list_claims(self, *, page: int, page_size: int) -> tuple[list[ChainClaim], int]:
        """Read one bounded page directly from the contract for diagnostic use.

        Claim IDs are contiguous and increasing, so walking backward returns the
        newest claims without fetching the full registry. The public FastAPI list
        uses PostgreSQL instead; this RPC method remains useful in focused tooling
        and tests where direct on-chain reads are specifically required.
        """

        try:
            claim_count = self.contract.functions.claimCount().call()
            claims: list[ChainClaim] = []
            # Claim IDs grow from zero. Walking backwards avoids reading the whole
            # registry just to render one dashboard page. A production application
            # would normally serve this view from an event index instead.
            first_claim_id = claim_count - 1 - ((page - 1) * page_size)
            if first_claim_id < 0:
                return claims, claim_count

            final_claim_id = max(first_claim_id - page_size + 1, 0)
            for claim_id in range(first_claim_id, final_claim_id - 1, -1):
                claim = self.contract.functions.getClaim(claim_id).call()
                claims.append(
                    ChainClaim(
                        claim_id=claim_id,
                        claimant=Web3.to_checksum_address(claim[0]),
                        claim_hash=_hex(claim[1]),
                        data_pointer=claim[2],
                        status=claim[3],
                        fraud_score=claim[4],
                        submitted_at=claim[5],
                        updated_at=claim[6],
                    )
                )
            return claims, claim_count
        except Exception as exc:
            raise BlockchainSubmissionError(
                f"Could not read claims from Sepolia: {exc}"
            ) from exc

    def claim_count(self, *, block_identifier: int | None = None) -> int:
        """Read the on-chain registry size at an optional snapshot block.

        Pinning the call is essential during reconciliation: otherwise new claims
        mined after the database checkpoint could create false missing-row reports.
        """

        try:
            call = self.contract.functions.claimCount()
            value = (
                call.call()
                if block_identifier is None
                else call.call(block_identifier=block_identifier)
            )
            return int(value)
        except Exception as exc:
            raise BlockchainSubmissionError(
                "Could not read the Sepolia claim count"
            ) from exc

    def get_claim(
        self, claim_id: int, *, block_identifier: int | None = None
    ) -> ChainClaim:
        """Read one on-chain claim at an optional snapshot block.

        Reconciliation passes the stored checkpoint so all calls describe the
        same historical state. Workers may omit it when checking current contract
        state before retrying an at-least-once write.
        """

        try:
            call = self.contract.functions.getClaim(claim_id)
            claim = (
                call.call()
                if block_identifier is None
                else call.call(block_identifier=block_identifier)
            )
            return ChainClaim(
                claim_id=claim_id,
                claimant=Web3.to_checksum_address(claim[0]),
                claim_hash=_hex(claim[1]),
                data_pointer=claim[2],
                status=claim[3],
                fraud_score=claim[4],
                submitted_at=claim[5],
                updated_at=claim[6],
            )
        except Exception as exc:
            raise BlockchainSubmissionError(
                f"Could not read claim {claim_id} from Sepolia: {exc}"
            ) from exc
