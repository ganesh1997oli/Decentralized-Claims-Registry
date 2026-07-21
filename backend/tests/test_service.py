from backend.app.blockchain import ChainSubmission
from backend.app.models import ClaimSubmission
from backend.app.service import (
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
    canonical_claim_bytes,
)


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
    def __init__(self):
        self.submission = None

    def submit_claim(self, claim_hash, data_pointer):
        self.submission = (claim_hash, data_pointer)
        return ChainSubmission(
            claim_id=3,
            transaction_hash="0xtransaction",
            block_number=100,
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
    service = ClaimSubmissionService(ipfs=ipfs, registry=registry)

    result = service.submit(claim_model())

    submitted_hash, submitted_pointer = registry.submission
    assert submitted_pointer == "ipfs://bafy-test"
    assert result.claim_id == 3
    assert result.data_pointer == submitted_pointer
    assert result.claim_hash == submitted_hash.hex()


def test_service_refuses_to_anchor_corrupt_ipfs_round_trip():
    registry = FakeRegistry()
    service = ClaimSubmissionService(
        ipfs=FakeIPFS(corrupt_download=True), registry=registry
    )

    try:
        service.submit(claim_model())
    except ClaimSubmissionServiceError as exc:
        assert "different" in str(exc)
    else:
        raise AssertionError("Expected ClaimSubmissionServiceError")

    assert registry.submission is None
