import threading
from types import SimpleNamespace

import pytest
from web3.exceptions import Web3RPCError

from apps.backend.app.blockchain import BlockchainSubmissionError, SepoliaClaimsRegistry


class FakeFunction:
    def __init__(self, built_nonces):
        self.built_nonces = built_nonces

    def build_transaction(self, transaction):
        self.built_nonces.append(transaction["nonce"])
        return transaction


class FakeContract:
    def __init__(self, built_nonces):
        function = FakeFunction(built_nonces)
        self.functions = SimpleNamespace(
            submitClaim=lambda _claim_hash, _data_pointer: function
        )
        event = SimpleNamespace(
            process_receipt=lambda _receipt: [{"args": {"claimId": 9}}]
        )
        self.events = SimpleNamespace(ClaimSubmitted=lambda: event)


class FakeAssessmentContract:
    def __init__(self, built_nonces):
        function = FakeFunction(built_nonces)
        self.functions = SimpleNamespace(
            assessClaim=lambda _claim_id, _status, _fraud_score: function
        )
        event = SimpleNamespace(
            process_receipt=lambda _receipt: [
                {
                    "args": {
                        "claimId": 9,
                        "newStatus": 4,
                        "fraudScore": 8500,
                    }
                }
            ]
        )
        self.events = SimpleNamespace(ClaimAssessed=lambda: event)


class FakeReadCall:
    def __init__(self, value, block_identifiers=None):
        self.value = value
        self.block_identifiers = block_identifiers

    def call(self, *, block_identifier=None):
        if self.block_identifiers is not None:
            self.block_identifiers.append(block_identifier)
        return self.value


class FakeReadContract:
    def __init__(self):
        self.block_identifiers = []
        claims = {
            0: (
                "0x0000000000000000000000000000000000000001",
                b"\x01" * 32,
                "ipfs://claim-zero",
                1,
                1200,
                100,
                101,
            ),
            1: (
                "0x0000000000000000000000000000000000000002",
                b"\x02" * 32,
                "ipfs://claim-one",
                4,
                8500,
                200,
                201,
            ),
        }
        self.functions = SimpleNamespace(
            claimCount=lambda: FakeReadCall(2, self.block_identifiers),
            getClaim=lambda claim_id: FakeReadCall(
                claims[claim_id], self.block_identifiers
            ),
        )


class FakeAccount:
    address = "0x0000000000000000000000000000000000000001"

    @staticmethod
    def sign_transaction(transaction):
        return SimpleNamespace(raw_transaction=transaction)


class StaleNonceEth:
    @staticmethod
    def get_transaction_count(_address, _block_identifier):
        return 6

    @staticmethod
    def send_raw_transaction(raw_transaction):
        if raw_transaction["nonce"] == 6:
            raise Web3RPCError(
                {
                    "code": -32000,
                    "message": "nonce too low: next nonce 7, tx nonce 6",
                }
            )
        return b"\x01"

    @staticmethod
    def wait_for_transaction_receipt(_transaction_hash, *, timeout):
        assert timeout == 180
        return {"status": 1, "blockNumber": 100}


def test_read_only_factory_does_not_require_or_load_a_private_key(monkeypatch):
    """Browsing public contract state must not need access to a wallet."""

    captured = {}

    class ReadOnlyProbe(SepoliaClaimsRegistry):
        def __init__(
            self,
            *,
            rpc_url,
            private_key,
            deployment,
            access,
            receipt_timeout,
            private_key_env,
        ):
            captured.update(
                rpc_url=rpc_url,
                private_key=private_key,
                deployment=deployment,
                access=access,
                receipt_timeout=receipt_timeout,
                private_key_env=private_key_env,
            )

    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.setenv(
        "CLAIMS_DEPLOYMENT_ID", "sepolia-security-audit-v1"
    )
    monkeypatch.delenv("SEPOLIA_SUBMITTER_PRIVATE_KEY", raising=False)

    registry = ReadOnlyProbe.from_env(require_private_key=False)

    assert isinstance(registry, ReadOnlyProbe)
    assert captured["rpc_url"] == "https://rpc.example.test"
    assert captured["private_key"] is None
    assert captured["access"] == "read"
    assert captured["deployment"].deployment_id == "sepolia-security-audit-v1"


def test_write_factory_still_requires_a_private_key(monkeypatch):
    """Transaction-capable clients must fail closed when no signer is supplied."""

    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.delenv("SEPOLIA_SUBMITTER_PRIVATE_KEY", raising=False)

    try:
        SepoliaClaimsRegistry.from_env()
    except BlockchainSubmissionError as exc:
        assert "SEPOLIA_SUBMITTER_PRIVATE_KEY" in str(exc)
    else:
        raise AssertionError("Expected the write-capable factory to require a key")


def test_production_write_factory_reads_a_mounted_private_key(tmp_path, monkeypatch):
    """Hosted writers keep raw wallet keys out of process environment values."""

    captured = {}

    class WriteProbe(SepoliaClaimsRegistry):
        def __init__(
            self,
            *,
            rpc_url,
            private_key,
            deployment,
            access,
            receipt_timeout,
            private_key_env,
        ):
            captured.update(
                rpc_url=rpc_url,
                private_key=private_key,
                deployment=deployment,
                access=access,
                receipt_timeout=receipt_timeout,
                private_key_env=private_key_env,
            )

    key_file = tmp_path / "assessor.key"
    key_file.write_text("0x" + "11" * 32, encoding="utf-8")
    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.setenv("CLAIMS_DEPLOYMENT_ID", "sepolia-public-intake-v1")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("SEPOLIA_ASSESSOR_PRIVATE_KEY_FILE", str(key_file))
    monkeypatch.delenv("SEPOLIA_ASSESSOR_PRIVATE_KEY", raising=False)

    registry = WriteProbe.from_env(
        private_key_env="SEPOLIA_ASSESSOR_PRIVATE_KEY"
    )

    assert isinstance(registry, WriteProbe)
    assert captured["private_key"] == "0x" + "11" * 32
    assert captured["access"] == "assessor"


def test_production_write_factory_rejects_environment_private_key(monkeypatch):
    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("SEPOLIA_ASSESSOR_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("SEPOLIA_ASSESSOR_PRIVATE_KEY_FILE", raising=False)

    with pytest.raises(BlockchainSubmissionError, match="must use.*_FILE"):
        SepoliaClaimsRegistry.from_env(
            private_key_env="SEPOLIA_ASSESSOR_PRIVATE_KEY"
        )


def test_write_factory_rejects_two_private_key_sources(tmp_path, monkeypatch):
    key_file = tmp_path / "assessor.key"
    key_file.write_text("0x" + "11" * 32, encoding="utf-8")
    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.setenv("SEPOLIA_ASSESSOR_PRIVATE_KEY", "0x" + "22" * 32)
    monkeypatch.setenv("SEPOLIA_ASSESSOR_PRIVATE_KEY_FILE", str(key_file))

    with pytest.raises(BlockchainSubmissionError, match="only one"):
        SepoliaClaimsRegistry.from_env(
            private_key_env="SEPOLIA_ASSESSOR_PRIVATE_KEY"
        )


def test_read_factory_requires_an_explicit_deployment(monkeypatch):
    monkeypatch.setenv("SEPOLIA_RPC_URL", "https://rpc.example.test")
    monkeypatch.delenv("CLAIMS_DEPLOYMENT_ID", raising=False)

    with pytest.raises(BlockchainSubmissionError, match="CLAIMS_DEPLOYMENT_ID"):
        SepoliaClaimsRegistry.from_env(require_private_key=False)


def test_submitter_access_fails_closed_when_role_is_missing():
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.account = FakeAccount()
    registry.private_key_env = "SEPOLIA_SUBMITTER_PRIVATE_KEY"
    registry.deployment = SimpleNamespace(deployment_id="hardened")
    registry.contract = SimpleNamespace(
        functions=SimpleNamespace(
            isSubmitter=lambda _address: FakeReadCall(False)
        )
    )

    with pytest.raises(BlockchainSubmissionError, match="not an authorized"):
        registry._verify_signer_access("submitter")


def test_registry_retries_nonce_reported_by_rpc():
    built_nonces = []
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.w3 = SimpleNamespace(eth=StaleNonceEth())
    registry.account = FakeAccount()
    registry.contract = FakeContract(built_nonces)
    registry.receipt_timeout = 180
    registry._submission_lock = threading.Lock()
    registry._next_nonce = None

    result = registry.submit_claim(b"hash", "ipfs://bafy-test")

    assert built_nonces == [6, 7]
    assert result.claim_id == 9
    assert result.transaction_hash == "0x01"
    assert registry._next_nonce == 8


def test_registry_assessment_reuses_next_nonce_and_validates_event():
    built_nonces = []
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.w3 = SimpleNamespace(eth=StaleNonceEth())
    registry.account = FakeAccount()
    registry.contract = FakeAssessmentContract(built_nonces)
    registry.receipt_timeout = 180
    registry._submission_lock = threading.Lock()
    registry._next_nonce = 7

    result = registry.assess_claim(9, 4, 8500)

    assert built_nonces == [7]
    assert result.status == 4
    assert result.fraud_score == 8500
    assert result.transaction_hash == "0x01"
    assert registry._next_nonce == 8


def test_registry_lists_all_claims_newest_first():
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.contract = FakeReadContract()

    claims, total = registry.list_claims(page=1, page_size=1)

    assert total == 2
    assert [claim.claim_id for claim in claims] == [1]
    assert claims[0].status == 4
    assert claims[0].fraud_score == 8500
    assert claims[0].claim_hash == f"0x{'02' * 32}"

    second_page, total = registry.list_claims(page=2, page_size=1)
    assert total == 2
    assert [claim.claim_id for claim in second_page] == [0]

    empty_page, total = registry.list_claims(page=3, page_size=1)
    assert total == 2
    assert empty_page == []


def test_registry_reads_one_claim_for_idempotency_checks():
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.contract = FakeReadContract()

    claim = registry.get_claim(1)

    assert claim.claim_id == 1
    assert claim.status == 4
    assert claim.fraud_score == 8500


def test_reconciliation_reads_are_pinned_to_one_block():
    registry = SepoliaClaimsRegistry.__new__(SepoliaClaimsRegistry)
    registry.contract = FakeReadContract()

    assert registry.claim_count(block_identifier=120) == 2
    claim = registry.get_claim(1, block_identifier=120)

    assert claim.claim_id == 1
    assert registry.contract.block_identifiers == [120, 120]
