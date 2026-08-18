"""Behavioral tests for maker/checker coverage-decision preparation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from apps.backend.app.coverage_governance import (
    CoverageGovernanceAccessError,
    CoverageGovernanceError,
    CoverageGovernanceService,
    CoverageGovernanceStateError,
)
from apps.backend.app.governance_auth import GovernancePrincipal
from packages.integrations.postgres import (
    AssessmentRecord,
    AssessorOutcomeRecord,
    CoverageDecisionProposalRecord,
    IndexedClaim,
)

CONTRACT = "0x1111111111111111111111111111111111111111"
INSURER = "0x2222222222222222222222222222222222222222"
MAKER = "0x3333333333333333333333333333333333333333"
OUTCOME_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeCall:
    def __init__(self, value) -> None:
        self.value = value

    def call(self):
        return self.value


class FakeContract:
    def __init__(
        self,
        *,
        maker_authorized: bool = True,
        encoding_error: Exception | None = None,
    ) -> None:
        self.maker_authorized = maker_authorized
        self.encoding_error = encoding_error
        self.functions = self

    def getClaimParties(self, _claim_id: int) -> FakeCall:
        return FakeCall((INSURER, MAKER, bytes(32)))

    def isDecisionMakerFor(self, _maker: str, _insurer: str) -> FakeCall:
        return FakeCall(self.maker_authorized)

    def encode_abi(self, function_name: str, *, args) -> str:
        if self.encoding_error is not None:
            raise self.encoding_error
        assert function_name == "decideClaim"
        assert args[0:2] == [7, 2]
        assert len(args[2]) == 32
        return "0x1234"


class FakeDecisionRepository:
    def __init__(self) -> None:
        self.values = None

    def create_or_get(self, **values):
        self.values = values
        return CoverageDecisionProposalRecord(
            decision_id=values["decision_id"],
            chain_id=values["chain_id"],
            contract_address=values["contract_address"].lower(),
            claim_id=values["claim_id"],
            decision_status=values["decision_status"],
            decision_hash=values["decision_hash"],
            decision_maker_address=values["decision_maker_address"].lower(),
            proposed_by=values["proposed_by"],
            human_outcome_id=values["human_outcome_id"],
            human_outcome_revision=values["human_outcome_revision"],
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
            confirmed_transaction_hash=None,
            confirmed_at=None,
        )


def fixture(
    *,
    outcome: str = "Legitimate",
    claim_status: int = 1,
    contract: FakeContract | None = None,
):
    claim = IndexedClaim(
        claim_id=7,
        claimant=MAKER,
        claim_hash="0x" + "44" * 32,
        data_pointer="ipfs://bafytest",
        status=claim_status,
        fraud_score=4200,
        submitted_at=1,
        updated_at=2,
    )
    assessment = AssessmentRecord(
        event_id="11155111:0xscreening:1",
        chain_id=11_155_111,
        contract_address=CONTRACT,
        claim_id=7,
        model_version="african-motor-xgboost-v1",
        probability=0.42,
        threshold=0.47,
        fraud_score=4200,
        status="UnderReview",
        reasons=(),
        processing_status="completed",
    )
    human = AssessorOutcomeRecord(
        outcome_id=OUTCOME_ID,
        chain_id=11_155_111,
        contract_address=CONTRACT,
        claim_id=7,
        revision=2,
        outcome=outcome,  # type: ignore[arg-type]
        assessor_reference="human-reviewer-1",
        notes="private notes are not copied into the public hash payload",
        assessed_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    decisions = FakeDecisionRepository()
    repositories = SimpleNamespace(
        claims=SimpleNamespace(get_claim=lambda **_query: claim),
        assessments=SimpleNamespace(get_latest_for_claim=lambda **_query: assessment),
        assessor_outcomes=SimpleNamespace(get_latest_for_claim=lambda **_query: human),
        coverage_decisions=decisions,
    )
    deployment = SimpleNamespace(
        chain_id=11_155_111,
        address=CONTRACT,
        require_governance=lambda: None,
    )
    service = CoverageGovernanceService(
        deployment=deployment,
        contract=contract or FakeContract(),
        repositories=repositories,
    )
    return service, decisions


def test_prepare_binds_evidence_and_returns_non_custodial_calldata():
    service, decisions = fixture()

    prepared = service.prepare(
        claim_id=7,
        decision_status="Approved",
        decision_maker_address=MAKER,
        principal=GovernancePrincipal("coverage-maker-1", INSURER),
    )

    assert prepared.transaction_data == "0x1234"
    assert decisions.values["human_outcome_id"] == OUTCOME_ID
    assert decisions.values["human_outcome_revision"] == 2
    assert decisions.values["decision_hash"].startswith("0x")
    assert len(decisions.values["decision_hash"]) == 66


def test_confirmed_fraud_cannot_be_proposed_for_approval():
    service, _ = fixture(outcome="ConfirmedFraud")

    with pytest.raises(CoverageGovernanceStateError, match="confirmed fraud"):
        service.prepare(
            claim_id=7,
            decision_status="Approved",
            decision_maker_address=MAKER,
            principal=GovernancePrincipal("coverage-maker-1", INSURER),
        )


def test_terminal_claim_cannot_receive_another_proposal():
    service, _ = fixture(claim_status=2)

    with pytest.raises(CoverageGovernanceStateError, match="UnderReview or Flagged"):
        service.prepare(
            claim_id=7,
            decision_status="Rejected",
            decision_maker_address=MAKER,
            principal=GovernancePrincipal("coverage-maker-1", INSURER),
        )


def test_proposal_maker_and_decision_wallet_must_both_match_insurer_scope():
    wrong_insurer_service, _ = fixture()
    with pytest.raises(CoverageGovernanceAccessError, match="credential"):
        wrong_insurer_service.prepare(
            claim_id=7,
            decision_status="Rejected",
            decision_maker_address=MAKER,
            principal=GovernancePrincipal(
                "other-insurer-maker",
                "0x4444444444444444444444444444444444444444",
            ),
        )

    unauthorized_wallet_service, _ = fixture(
        contract=FakeContract(maker_authorized=False)
    )
    with pytest.raises(CoverageGovernanceAccessError, match="connected wallet"):
        unauthorized_wallet_service.prepare(
            claim_id=7,
            decision_status="Rejected",
            decision_maker_address=MAKER,
            principal=GovernancePrincipal("coverage-maker-1", INSURER),
        )


def test_invalid_deployment_calldata_does_not_persist_an_unusable_proposal():
    service, decisions = fixture(
        contract=FakeContract(encoding_error=ValueError("missing ABI"))
    )

    with pytest.raises(CoverageGovernanceError, match="encode"):
        service.prepare(
            claim_id=7,
            decision_status="Approved",
            decision_maker_address=MAKER,
            principal=GovernancePrincipal("coverage-maker-1", INSURER),
        )

    assert decisions.values is None
