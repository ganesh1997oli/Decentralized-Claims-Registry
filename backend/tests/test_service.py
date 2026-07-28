from backend.app.blockchain import (
    ChainClaim,
    ChainSubmission,
)
from backend.app.models import ClaimSubmission
from backend.app.service import (
    ClaimQueryService,
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
    canonical_claim_bytes,
)


def claim_model() -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            "insurerId": "northstar-mutual",
            "claimReference": "synthetic-claim-api-1",
            "policyReference": "synthetic-policy-42",
            "claimType": "collision",
            "incidentDate": "2026-07-13",
            "claimAmountUsd": 2500,
            "policyPremiumUsd": 480,
            "vehicleAge": 6,
            "vehicleType": "sedan",
            "country": "Nigeria",
            "regionType": "urban",
            "thirdPartyInjuryFlag": False,
            "totalLossFlag": False,
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
    def __init__(self):
        self.submission = None

    def submit_claim(self, claim_hash, data_pointer):
        self.submission = (claim_hash, data_pointer)
        return ChainSubmission(
            claim_id=3,
            transaction_hash="0xtransaction",
            block_number=100,
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


def test_canonical_serialization_is_stable():
    payload = canonical_claim_bytes(claim_model())

    assert payload == (
        b'{"claimAmountUsd":2500.0,"claimReference":"synthetic-claim-api-1",'
        b'"claimType":"collision","country":"Nigeria","description":"Synthetic '
        b'bumper damage for API testing","evidence":[],"incidentDate":"2026-07-13",'
        b'"insurerId":"northstar-mutual","policyPremiumUsd":480.0,'
        b'"policyReference":"synthetic-policy-42","regionType":"urban",'
        b'"schemaVersion":3,"thirdPartyInjuryFlag":false,'
        b'"totalLossFlag":false,"vehicleAge":6,"vehicleType":"sedan"}'
    )


def test_service_uploads_verifies_and_submits_exact_payload():
    ipfs = FakeIPFS()
    registry = FakeRegistry()
    service = ClaimSubmissionService(ipfs=ipfs, registry=registry)

    result = service.submit(claim_model())

    submitted_hash, submitted_pointer = registry.submission
    assert submitted_pointer == "ipfs://bafy-test"
    assert result.claim_id == 3
    assert result.data_pointer == submitted_pointer
    assert result.claim_hash == submitted_hash.hex()
    assert result.assessment is None


def test_service_refuses_to_anchor_corrupt_ipfs_round_trip():
    registry = FakeRegistry()
    service = ClaimSubmissionService(
        ipfs=FakeIPFS(corrupt_download=True),
        registry=registry,
    )

    try:
        service.submit(claim_model())
    except ClaimSubmissionServiceError as exc:
        assert "different" in str(exc)
    else:
        raise AssertionError("Expected ClaimSubmissionServiceError")

    assert registry.submission is None


def test_service_lists_current_claim_state():
    service = ClaimQueryService(registry=FakeRegistry())

    claims = service.list_claims(page=1, page_size=10)

    assert len(claims.items) == 1
    assert claims.items[0].claim_id == 3
    assert claims.items[0].status == "Flagged"
    assert claims.items[0].fraud_score == 8500
    assert claims.total_items == 14
    assert claims.total_pages == 2
