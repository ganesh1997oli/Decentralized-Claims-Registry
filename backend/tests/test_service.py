from backend.app.blockchain import (
    BlockchainSubmissionError,
    ChainAssessment,
    ChainClaim,
    ChainSubmission,
)
from backend.app.models import ClaimSubmission
from backend.app.service import (
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
    canonical_claim_bytes,
)
from model.scorer import FraudReason, FraudScore


def claim_model() -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            "claimReference": "synthetic-claim-api-1",
            "policyReference": "synthetic-policy-42",
            "claimType": "vehicle_damage",
            "incidentDate": "2026-07-13",
            "amountPence": 250000,
            "description": "Synthetic bumper damage for API testing",
            "evidence": [],
        }
    )


class FakeIPFS:
    def __init__(self, *, corrupt_download: bool = False):
        self.payload = None
        self.corrupt_download = corrupt_download

    def upload_bytes(self, payload, *, filename, content_type):
        self.payload = payload
        assert filename == "synthetic-claim-api-1.json"
        assert content_type == "application/json"
        return "bafy-test"

    def download_pointer(self, pointer, *, attempts=3):
        assert pointer == "ipfs://bafy-test"
        return b"corrupt" if self.corrupt_download else self.payload


class FakeRegistry:
    def __init__(self, *, fail_assessment: bool = False):
        self.submission = None
        self.assessment = None
        self.fail_assessment = fail_assessment

    def submit_claim(self, claim_hash, data_pointer):
        self.submission = (claim_hash, data_pointer)
        return ChainSubmission(
            claim_id=3,
            transaction_hash="0xtransaction",
            block_number=100,
        )

    def assess_claim(self, claim_id, status, fraud_score):
        self.assessment = (claim_id, status, fraud_score)
        if self.fail_assessment:
            raise BlockchainSubmissionError("temporary RPC failure")
        return ChainAssessment(
            transaction_hash="0xassessment",
            block_number=101,
            status=status,
            fraud_score=fraud_score,
        )

    def list_claims(self, *, page, page_size):
        assert page == 1
        assert page_size == 10
        return (
            [
                ChainClaim(
                    claim_id=3,
                    claimant="0x0000000000000000000000000000000000000001",
                    claim_hash="0xhash",
                    data_pointer="ipfs://bafy-test",
                    status=4,
                    fraud_score=8500,
                    submitted_at=1_750_000_000,
                    updated_at=1_750_000_010,
                )
            ],
            14,
        )


class FakeScorer:
    def __init__(self, *, flagged: bool = True):
        self.flagged = flagged

    def score(self, claim):
        assert claim.claim_reference == "synthetic-claim-api-1"
        return FraudScore(
            probability=0.85 if self.flagged else 0.12,
            score_basis_points=8500 if self.flagged else 1200,
            threshold=0.3,
            flagged=self.flagged,
            model_version="test-model-v1",
            reasons=(FraudReason("amount_ratio", "Claim amount", 0.5),),
        )


def test_canonical_serialization_is_stable():
    payload = canonical_claim_bytes(claim_model())

    assert payload == (
        b'{"amountPence":250000,"claimReference":"synthetic-claim-api-1",'
        b'"claimType":"vehicle_damage","description":"Synthetic bumper damage '
        b'for API testing","evidence":[],"incidentDate":"2026-07-13",'
        b'"policyReference":"synthetic-policy-42","schemaVersion":1}'
    )


def test_service_uploads_verifies_and_submits_exact_payload():
    ipfs = FakeIPFS()
    registry = FakeRegistry()
    service = ClaimSubmissionService(
        ipfs=ipfs, registry=registry, scorer=FakeScorer()
    )

    result = service.submit(claim_model())

    submitted_hash, submitted_pointer = registry.submission
    assert submitted_pointer == "ipfs://bafy-test"
    assert result.claim_id == 3
    assert result.data_pointer == submitted_pointer
    assert result.claim_hash == submitted_hash.hex()
    assert registry.assessment == (3, 4, 8500)
    assert result.assessment.status == "Flagged"
    assert result.assessment.on_chain is True
    assert result.assessment.transaction_hash == "0xassessment"


def test_service_refuses_to_anchor_corrupt_ipfs_round_trip():
    registry = FakeRegistry()
    service = ClaimSubmissionService(
        ipfs=FakeIPFS(corrupt_download=True),
        registry=registry,
        scorer=FakeScorer(),
    )

    try:
        service.submit(claim_model())
    except ClaimSubmissionServiceError as exc:
        assert "different" in str(exc)
    else:
        raise AssertionError("Expected ClaimSubmissionServiceError")

    assert registry.submission is None


def test_service_returns_anchor_when_assessment_transaction_is_pending():
    registry = FakeRegistry(fail_assessment=True)
    service = ClaimSubmissionService(
        ipfs=FakeIPFS(),
        registry=registry,
        scorer=FakeScorer(flagged=False),
    )

    result = service.submit(claim_model())

    assert result.claim_id == 3
    assert registry.assessment == (3, 1, 1200)
    assert result.assessment.status == "UnderReview"
    assert result.assessment.on_chain is False
    assert "pending" in result.assessment.error


def test_service_lists_current_claim_state():
    service = ClaimSubmissionService(
        ipfs=FakeIPFS(),
        registry=FakeRegistry(),
        scorer=FakeScorer(),
    )

    claims = service.list_claims(page=1, page_size=10)

    assert len(claims.items) == 1
    assert claims.items[0].claim_id == 3
    assert claims.items[0].status == "Flagged"
    assert claims.items[0].fraud_score == 8500
    assert claims.total_items == 14
    assert claims.total_pages == 2
