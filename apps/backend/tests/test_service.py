from datetime import UTC, datetime

from apps.backend.app.blockchain import ChainSubmission
from apps.backend.app.models import ClaimSubmission
from apps.backend.app.service import (
    ClaimQueryService,
    ClaimSubmissionService,
    ClaimSubmissionServiceError,
    canonical_claim_bytes,
)
from apps.backend.app.submission_auth import ClaimAuthorizationSigner, InsurerPrincipal
from packages.integrations.postgres import ClaimIndexStatus, IndexedClaim

AUTHORIZATION = ClaimAuthorizationSigner(
    b"service-test-claim-authorization-key-32-bytes"
)
PRINCIPAL = InsurerPrincipal(
    insurer_id="northstar-mutual",
    credential_id="northstar-test-v1",
    signer_address="0x1111111111111111111111111111111111111111",
    permitted_operations=frozenset({"submit_claim"}),
    daily_quota=25,
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
        assert filename == "synthetic-claim-api-1.claim-envelope.json"
        assert content_type == "application/vnd.claims-registry.envelope+json"
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

    def list_claims(
        self, *, chain_id, contract_address, page, page_size
    ):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        assert page == 1
        assert page_size == 10
        return (
            [
                IndexedClaim(
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

    def get_status(self, *, chain_id, contract_address):
        assert chain_id == 11_155_111
        assert contract_address == "0xcontract"
        return ClaimIndexStatus(
            chain_id=chain_id,
            contract_address=contract_address,
            last_processed_block=11_400_000,
            updated_at=datetime(2026, 8, 4, tzinfo=UTC),
        )


class TestPrivacy:
    """Visible marker keeps orchestration tests independent from AES randomness."""

    def seal(self, plaintext):
        return b"encrypted:" + plaintext


def test_canonical_serialization_is_stable():
    first = canonical_claim_bytes(claim_model(), PRINCIPAL, AUTHORIZATION)
    second = canonical_claim_bytes(claim_model(), PRINCIPAL, AUTHORIZATION)

    assert first == second
    assert b'"schemaVersion":5' in first
    assert b'"credentialId":"northstar-test-v1"' in first
    assert b'"signerAddress":"0x1111111111111111111111111111111111111111"' in first
    assert b'"signature":' in first


def test_service_uploads_verifies_and_submits_exact_payload():
    ipfs = FakeIPFS()
    registry = FakeRegistry()
    service = ClaimSubmissionService(
        ipfs=ipfs,
        registry=registry,
        authorization=AUTHORIZATION,
        privacy=TestPrivacy(),
    )

    result = service.submit(claim_model(), PRINCIPAL)

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
        authorization=AUTHORIZATION,
        privacy=TestPrivacy(),
    )

    try:
        service.submit(claim_model(), PRINCIPAL)
    except ClaimSubmissionServiceError as exc:
        assert "different" in str(exc)
    else:
        raise AssertionError("Expected ClaimSubmissionServiceError")

    assert registry.submission is None


def test_service_lists_current_claim_state():
    service = ClaimQueryService(
        index=FakeRegistry(),
        chain_id=11_155_111,
        contract_address="0xcontract",
    )

    claims = service.list_claims(page=1, page_size=10)

    assert len(claims.items) == 1
    assert claims.items[0].claim_id == 3
    assert claims.items[0].status == "Flagged"
    assert claims.items[0].fraud_score == 8500
    assert claims.total_items == 14
    assert claims.total_pages == 2
    assert claims.indexed_through_block == 11_400_000
