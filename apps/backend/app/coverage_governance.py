"""Prepare maker/checker coverage decisions without custody of wallet keys."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from web3 import Web3

from apps.backend.app.governance_auth import GovernancePrincipal
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentValidationError,
    connect_claims_deployment,
    load_claims_deployment,
)
from packages.integrations.postgres import (
    AssessmentRecord,
    AssessorOutcomeRecord,
    CoverageDecisionProposalRecord,
    CoverageDecisionStatus,
    IndexedClaim,
    PostgresRepositories,
)


class CoverageGovernanceError(RuntimeError):
    """Base error for safe HTTP translation."""


class CoverageGovernanceStateError(CoverageGovernanceError):
    """Raised when lifecycle evidence is not ready or already terminal."""


class CoverageGovernanceAccessError(CoverageGovernanceError):
    """Raised when maker or checker authority does not match the claim."""


@dataclass(frozen=True)
class PreparedCoverageDecision:
    """Durable proposal plus exact wallet transaction fields."""

    proposal: CoverageDecisionProposalRecord
    chain_id: int
    contract_address: str
    transaction_data: str


class CoverageGovernanceService:
    """Validate prerequisites and prepare one non-custodial final decision.

    This module owns the whole policy seam: claim state, completed screening,
    latest human conclusion, API principal scope, decision-wallet scope,
    canonical audit hash, and calldata. Routes do not reimplement fragments of
    those rules, and the service never accepts or reads a decision private key.
    """

    def __init__(
        self,
        *,
        deployment: ClaimsDeployment,
        contract,
        repositories: PostgresRepositories,
    ) -> None:
        deployment.require_governance()
        self.deployment = deployment
        self.contract = contract
        self.repositories = repositories

    @classmethod
    def from_env(cls) -> CoverageGovernanceService:
        deployment = load_claims_deployment(os.environ)
        rpc_url = os.environ.get("SEPOLIA_RPC_URL") or os.environ.get("RPC_URL")
        if not rpc_url:
            raise CoverageGovernanceError("SEPOLIA_RPC_URL is required")
        try:
            contract = connect_claims_deployment(
                Web3(Web3.HTTPProvider(rpc_url)),
                deployment,
            )
        except DeploymentValidationError as exc:
            raise CoverageGovernanceError(str(exc)) from exc
        return cls(
            deployment=deployment,
            contract=contract,
            repositories=PostgresRepositories.from_env(),
        )

    def prepare(
        self,
        *,
        claim_id: int,
        decision_status: CoverageDecisionStatus,
        decision_maker_address: str,
        principal: GovernancePrincipal,
    ) -> PreparedCoverageDecision:
        query = {
            "chain_id": self.deployment.chain_id,
            "contract_address": self.deployment.address,
            "claim_id": claim_id,
        }
        claim = self.repositories.claims.get_claim(**query)
        screening = self.repositories.assessments.get_latest_for_claim(**query)
        outcome = self.repositories.assessor_outcomes.get_latest_for_claim(**query)
        self._validate_evidence(
            claim=claim,
            screening=screening,
            outcome=outcome,
            decision_status=decision_status,
        )
        assert claim is not None and screening is not None and outcome is not None

        normalized_maker = Web3.to_checksum_address(decision_maker_address)
        try:
            insurer, _, _ = self.contract.functions.getClaimParties(claim_id).call()
            normalized_insurer = Web3.to_checksum_address(insurer)
            maker_authorized = self.contract.functions.isDecisionMakerFor(
                normalized_maker,
                normalized_insurer,
            ).call()
        except Exception as exc:
            raise CoverageGovernanceError(
                "Could not verify decision authority against the active registry"
            ) from exc
        if normalized_insurer.lower() != principal.insurer_address:
            raise CoverageGovernanceAccessError(
                "The governance credential is not scoped to this claim's insurer"
            )
        if not maker_authorized:
            raise CoverageGovernanceAccessError(
                "The connected wallet is not an on-chain decision maker for this insurer"
            )

        identity = ":".join(
            (
                str(self.deployment.chain_id),
                self.deployment.address.lower(),
                str(claim_id),
                decision_status,
                normalized_maker.lower(),
                principal.governance_reference,
                str(outcome.outcome_id),
                str(outcome.revision),
            )
        )
        decision_id = uuid5(NAMESPACE_URL, f"claims-registry:decision:{identity}")
        canonical_record = {
            "version": "coverage-decision-v1",
            "decisionId": str(decision_id),
            "chainId": self.deployment.chain_id,
            "contractAddress": self.deployment.address.lower(),
            "claimId": claim_id,
            "decisionStatus": decision_status,
            "decisionMakerAddress": normalized_maker.lower(),
            "proposedBy": principal.governance_reference,
            "insurerAddress": normalized_insurer.lower(),
            "screening": {
                "eventId": screening.event_id,
                "modelVersion": screening.model_version,
                "status": screening.status,
                "fraudScore": screening.fraud_score,
            },
            "humanOutcome": {
                "outcomeId": str(outcome.outcome_id),
                "revision": outcome.revision,
                "outcome": outcome.outcome,
            },
        }
        canonical_bytes = json.dumps(
            canonical_record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        decision_hash = Web3.keccak(canonical_bytes).hex()
        if not decision_hash.startswith("0x"):
            decision_hash = f"0x{decision_hash}"

        status_value = 2 if decision_status == "Approved" else 3
        try:
            transaction_data = self.contract.encode_abi(
                "decideClaim",
                args=[claim_id, status_value, bytes.fromhex(decision_hash[2:])],
            )
        except Exception as exc:
            raise CoverageGovernanceError(
                "Could not encode the decision transaction"
            ) from exc
        # Encode before persistence. A deployment/ABI mismatch must not leave an
        # immutable proposal that no wallet can execute and that subsequently
        # blocks a corrected request for this claim.
        proposal = self.repositories.coverage_decisions.create_or_get(
            decision_id=decision_id,
            **query,
            decision_status=decision_status,
            decision_hash=decision_hash,
            decision_maker_address=normalized_maker,
            proposed_by=principal.governance_reference,
            human_outcome_id=outcome.outcome_id,
            human_outcome_revision=outcome.revision,
        )
        return PreparedCoverageDecision(
            proposal=proposal,
            chain_id=self.deployment.chain_id,
            contract_address=self.deployment.address,
            transaction_data=transaction_data,
        )

    @staticmethod
    def _validate_evidence(
        *,
        claim: IndexedClaim | None,
        screening: AssessmentRecord | None,
        outcome: AssessorOutcomeRecord | None,
        decision_status: CoverageDecisionStatus,
    ) -> None:
        if claim is None:
            raise CoverageGovernanceStateError(
                "Claim is not available in the confirmed index"
            )
        if claim.status not in {1, 4}:
            raise CoverageGovernanceStateError(
                "Only screened UnderReview or Flagged claims can be decided"
            )
        if screening is None or screening.processing_status != "completed":
            raise CoverageGovernanceStateError(
                "A confirmed model screening is required before a coverage decision"
            )
        if outcome is None or outcome.outcome == "Inconclusive":
            raise CoverageGovernanceStateError(
                "A conclusive human fraud review is required before a coverage decision"
            )
        if outcome.outcome == "ConfirmedFraud" and decision_status == "Approved":
            raise CoverageGovernanceStateError(
                "A claim with confirmed fraud cannot be proposed for approval"
            )
