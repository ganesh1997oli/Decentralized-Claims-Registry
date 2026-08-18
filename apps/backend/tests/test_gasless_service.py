from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from apps.backend.app.claimant_auth import ClaimantSession
from apps.backend.app.gasless_blockchain import PreparedForwardRequest
from apps.backend.app.gasless_service import (
    GaslessClaimSubmissionService,
    GaslessSubmissionAccessError,
)
from apps.backend.app.models import ClaimSubmission
from apps.backend.app.policy_eligibility import ClaimantPrincipal
from apps.backend.app.submission_auth import ClaimAuthorizationSigner, InsurerPrincipal
from packages.integrations.postgres import GaslessSubmissionRecord

SUBMISSION_ID = UUID("11111111-1111-4111-8111-111111111111")
SIGNER = "0x1111111111111111111111111111111111111111"
CONTRACT = "0x2222222222222222222222222222222222222222"
FORWARDER = "0x3333333333333333333333333333333333333333"
INSURER = "0x4444444444444444444444444444444444444444"
PERMIT_ISSUER = "0x5555555555555555555555555555555555555555"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
PRINCIPAL = InsurerPrincipal(
    insurer_id="northstar-mutual",
    credential_id="northstar-test-v1",
    signer_address=SIGNER,
    permitted_operations=frozenset({"submit_claim"}),
    daily_quota=25,
)
CLAIMANT_SESSION = ClaimantSession(
    subject_id="claimant-" + ("a" * 64),
    claimant_address=SIGNER,
    expires_at=NOW + timedelta(minutes=15),
)
PUBLIC_PRINCIPAL = ClaimantPrincipal(
    subject_id=CLAIMANT_SESSION.subject_id,
    claimant_address=SIGNER,
    submitter_address=SIGNER,
    claimant_commitment="0x" + ("aa" * 32),
    insurer_id="northstar-mutual",
    insurer_address=INSURER,
    policy_id="policy-internal-42",
    daily_quota=4,
)


def claim() -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            "insurerId": "northstar-mutual",
            "claimReference": "synthetic-gasless-1",
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
            "description": "Synthetic gasless service test",
            "evidence": [],
        }
    )


class FakeIPFS:
    def __init__(self):
        self.payload = None
        self.uploads = 0

    def upload_bytes(self, payload, *, filename, content_type):
        self.uploads += 1
        self.payload = payload
        assert filename == "synthetic-gasless-1.claim-envelope.json"
        assert content_type == "application/vnd.claims-registry.envelope+json"
        return "bafygaslesstest"

    def download_pointer(self, pointer, *, attempts=3):
        assert pointer == "ipfs://bafygaslesstest"
        return self.payload


class FakeChain:
    def __init__(self):
        self.deployment = SimpleNamespace(
            chain_id=11_155_111,
            address=CONTRACT,
            forwarder_address=FORWARDER,
        )
        self.verified = []
        self.prepared_permit_id = None

    def validate_signer(self, signer_address):
        assert signer_address == SIGNER
        return SIGNER

    def validate_principal(self, principal):
        return self.validate_signer(principal.signer_address)

    def permit_issuer_address(self, principal):
        assert principal.insurer_address == INSURER
        return PERMIT_ISSUER

    def prepare_request(self, *, principal, claim_hash, data_pointer, permit_id):
        assert principal.signer_address == SIGNER
        self.prepared_permit_id = permit_id
        assert len(claim_hash) == 32
        assert data_pointer == "ipfs://bafygaslesstest"
        return PreparedForwardRequest(
            from_address=SIGNER,
            to=CONTRACT,
            value=0,
            gas=250_000,
            nonce=7,
            deadline=2_000_000_000,
            data="0x1234",
        )

    def verify_signature(self, record, signature):
        assert record.state == "prepared"
        self.verified.append(signature)


class FakeStore:
    def __init__(self):
        self.record = None
        self.failed = False

    def begin_preparation(self, **values):
        if self.record is not None:
            return self.record, False
        self.record = GaslessSubmissionRecord(
            submission_id=values["submission_id"],
            credential_id=values["credential_id"],
            insurer_id=values["insurer_id"],
            signer_address=values["signer_address"],
            chain_id=values["chain_id"],
            contract_address=values["contract_address"],
            forwarder_address=values["forwarder_address"],
            idempotency_key_hash=values["idempotency_key_hash"],
            request_fingerprint=values["request_fingerprint"],
            client_fingerprint=values["client_fingerprint"],
            state="preparing",
            submission_kind=values["submission_kind"],
            claimant_address=values["claimant_address"],
            insurer_address=values["insurer_address"],
            claimant_commitment=values["claimant_commitment"],
            policy_id=values["policy_id"],
            permit_issuer_address=values["permit_issuer_address"],
            created_at=NOW,
            updated_at=NOW,
        )
        return self.record, True

    def mark_prepared(self, submission_id, **values):
        assert submission_id == SUBMISSION_ID
        self.record = replace(self.record, state="prepared", **values)
        return self.record

    def mark_preparation_failed(self, submission_id, *, error_code):
        assert submission_id == SUBMISSION_ID
        assert error_code == "preparation_failed"
        self.failed = True

    def get_for_credential(self, submission_id, *, credential_id):
        assert submission_id == SUBMISSION_ID
        if credential_id != self.record.credential_id:
            raise AssertionError("Test should use the owning credential")
        return self.record

    def authorize(self, submission_id, *, credential_id, signature, now):
        assert submission_id == SUBMISSION_ID
        assert credential_id == PRINCIPAL.credential_id
        assert now == NOW
        self.record = replace(
            self.record,
            state="authorized",
            insurer_signature=signature,
            authorized_at=now,
        )
        return self.record


class FakeEligibility:
    def verify(self, submitted_claim, session):
        assert submitted_claim.policy_reference == "synthetic-policy-42"
        assert session == CLAIMANT_SESSION
        return PUBLIC_PRINCIPAL


class TestPrivacy:
    """Make encrypted storage observable without coupling service tests to crypto."""

    def seal(self, plaintext):
        return b"encrypted:" + plaintext


def service(ipfs=None, chain=None, store=None, eligibility=None):
    return GaslessClaimSubmissionService(
        ipfs=ipfs or FakeIPFS(),
        chain=chain or FakeChain(),
        store=store or FakeStore(),
        authorization=ClaimAuthorizationSigner(
            b"gasless-service-authorization-key-32-bytes"
        ),
        privacy=TestPrivacy(),
        fingerprint_key=b"gasless-request-fingerprint-key-32-bytes",
        insurer_minute_limit=5,
        client_minute_limit=20,
        allow_rate_limit_bypass=False,
        eligibility=eligibility,
        clock=lambda: NOW,
        new_submission_id=lambda: SUBMISSION_ID,
    )


def test_prepare_returns_exact_wallet_domain_and_schema_v5_payload():
    ipfs = FakeIPFS()
    store = FakeStore()

    result = service(ipfs=ipfs, store=store).prepare(
        claim(),
        PRINCIPAL,
        idempotency_key="request-123",
        client_ip="192.0.2.10",
    )

    assert result.state == "prepared"
    assert result.typed_data is not None
    assert result.typed_data.domain.verifying_contract == FORWARDER
    assert result.typed_data.message.nonce == "7"
    assert result.typed_data.message.from_address == SIGNER
    assert ipfs.payload.startswith(b"encrypted:")
    assert b'"schemaVersion":5' in ipfs.payload
    assert f'"signerAddress":"{SIGNER}"'.encode() in ipfs.payload
    assert store.record.claim_hash == result.claim_hash


def test_network_preflight_matches_the_active_deployment():
    result = service().network()

    assert result.chain_id == 11_155_111
    assert result.contract_address == CONTRACT
    assert result.forwarder_address == FORWARDER
    assert result.domain_name == "ClaimsRegistryForwarder"
    assert result.domain_version == "1"


def test_public_prepare_persists_parties_and_schema_v6_authorization():
    ipfs = FakeIPFS()
    chain = FakeChain()
    store = FakeStore()

    result = service(
        ipfs=ipfs,
        chain=chain,
        store=store,
        eligibility=FakeEligibility(),
    ).prepare(
        claim(),
        CLAIMANT_SESSION,
        idempotency_key="public-request-123",
        client_ip="192.0.2.10",
    )

    assert result.state == "prepared"
    assert b'"schemaVersion":6' in ipfs.payload
    assert b'"claimant-policy-permit-hmac-sha256-v3"' in ipfs.payload
    assert store.record.submission_kind == "public"
    assert store.record.claimant_address == SIGNER
    assert store.record.insurer_address == INSURER
    assert store.record.policy_id == "policy-internal-42"
    assert store.record.permit_issuer_address == PERMIT_ISSUER
    assert chain.prepared_permit_id is not None


def test_prepare_is_idempotent_and_does_not_upload_twice():
    ipfs = FakeIPFS()
    store = FakeStore()
    subject = service(ipfs=ipfs, store=store)

    first = subject.prepare(
        claim(), PRINCIPAL, idempotency_key="request-123", client_ip="192.0.2.10"
    )
    second = subject.prepare(
        claim(), PRINCIPAL, idempotency_key="request-123", client_ip="192.0.2.10"
    )

    assert first == second
    assert ipfs.uploads == 1


def test_authorize_verifies_signature_before_enqueuing():
    chain = FakeChain()
    store = FakeStore()
    subject = service(chain=chain, store=store)
    subject.prepare(
        claim(), PRINCIPAL, idempotency_key="request-123", client_ip="192.0.2.10"
    )
    signature = "0x" + ("ab" * 65)

    result = subject.authorize(SUBMISSION_ID, signature, PRINCIPAL)

    assert chain.verified == [signature]
    assert result.state == "authorized"
    assert result.typed_data is None


def test_authorize_rejects_a_different_configured_signer():
    store = FakeStore()
    subject = service(store=store)
    subject.prepare(
        claim(), PRINCIPAL, idempotency_key="request-123", client_ip="192.0.2.10"
    )
    changed = replace(
        PRINCIPAL,
        signer_address="0x4444444444444444444444444444444444444444",
    )

    with pytest.raises(GaslessSubmissionAccessError, match="claimant session"):
        subject.authorize(SUBMISSION_ID, "0x" + ("ab" * 65), changed)


def test_confirmed_status_returns_the_public_chain_receipt():
    store = FakeStore()
    subject = service(store=store)
    subject.prepare(
        claim(), PRINCIPAL, idempotency_key="request-123", client_ip="192.0.2.10"
    )
    store.record = replace(
        store.record,
        state="confirmed",
        insurer_signature="0x" + ("ab" * 65),
        transaction_hash="0xtransaction",
        block_number=123,
        claim_id=9,
    )

    result = subject.status(SUBMISSION_ID, PRINCIPAL)

    assert result.receipt is not None
    assert result.receipt.claim_id == 9
    assert result.receipt.transaction_hash == "0xtransaction"
