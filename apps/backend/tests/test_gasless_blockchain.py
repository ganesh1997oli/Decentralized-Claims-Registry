"""Focused unit tests for the keyless gateway and restricted relay adapter."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from hexbytes import HexBytes

import apps.backend.app.gasless_blockchain as gasless
from apps.backend.app.gasless_blockchain import (
    GaslessBlockchainError,
    GaslessClaimsGateway,
    GaslessRelayChain,
    PreparedForwardRequest,
)
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentConfigurationError,
)
from packages.integrations.postgres import GaslessSubmissionRecord

SIGNER = "0x1111111111111111111111111111111111111111"
REGISTRY = "0x2222222222222222222222222222222222222222"
FORWARDER = "0x3333333333333333333333333333333333333333"
RELAYER = "0x4444444444444444444444444444444444444444"
ADMIN = "0x5555555555555555555555555555555555555555"
CLAIM_HASH = "0x" + ("12" * 32)
TRANSACTION_HASH = "0x" + ("34" * 32)
SIGNATURE = "0x" + ("ab" * 65)

DEPLOYMENT = ClaimsDeployment(
    deployment_id="sepolia-gasless-test",
    chain_id=11_155_111,
    address=REGISTRY,
    abi=({"type": "function", "name": "submitClaim"},),
    forwarder_address=FORWARDER,
    forwarder_abi=({"type": "function", "name": "execute"},),
)


def submission_record(**changes) -> GaslessSubmissionRecord:
    """Build one complete durable authorization and allow focused overrides."""

    record = GaslessSubmissionRecord(
        submission_id=UUID("11111111-1111-4111-8111-111111111111"),
        credential_id="northstar-test-v1",
        insurer_id="northstar-mutual",
        signer_address=SIGNER,
        chain_id=DEPLOYMENT.chain_id,
        contract_address=REGISTRY,
        forwarder_address=FORWARDER,
        idempotency_key_hash="a" * 64,
        client_fingerprint="b" * 64,
        state="authorized",
        claim_hash=CLAIM_HASH,
        data_pointer="ipfs://bafy-test",
        call_data="0x1234",
        forwarder_nonce=7,
        forward_gas=250_000,
        deadline=2_000_000_000,
        insurer_signature=SIGNATURE,
    )
    return replace(record, **changes)


class FakeCall:
    """Mimic the final ``.call()`` stage of a Web3 contract function."""

    def __init__(self, value=None, *, error: Exception | None = None):
        self.value = value
        self.error = error

    def call(self):
        if self.error is not None:
            raise self.error
        return self.value


class FakeGatewayFunctions:
    """Expose the registry and forwarder reads used during preparation."""

    def __init__(self):
        self.submitter = True
        self.signature_valid = True
        self.nonce = 7
        self.verify_error: Exception | None = None
        self.verified_request = None

    def isSubmitter(self, _address):
        return FakeCall(self.submitter)

    def nonces(self, _address):
        return FakeCall(self.nonce)

    def verify(self, request):
        self.verified_request = request
        return FakeCall(self.signature_valid, error=self.verify_error)


class FakeRegistry:
    """Capture ABI encoding and provide configurable contract calls/events."""

    def __init__(self, functions=None, *, events=None):
        self.functions = functions or FakeGatewayFunctions()
        self.events = events
        self.encoded = None

    def encode_abi(self, name, *, args):
        self.encoded = (name, args)
        return "0x1234"


class FakeExecute:
    """Represent the allowlisted forwarder execute call before signing."""

    def __init__(self, *, estimate=100_000, error: Exception | None = None):
        self.estimate = estimate
        self.error = error
        self.built = None

    def estimate_gas(self, transaction):
        assert transaction == {"from": RELAYER, "value": 0}
        if self.error is not None:
            raise self.error
        return self.estimate

    def build_transaction(self, transaction):
        self.built = transaction
        return transaction


class FakeRelayFunctions(FakeGatewayFunctions):
    """Add least-privilege reads and executable-call construction."""

    def __init__(self, execute: FakeExecute | None = None):
        super().__init__()
        self.execute_call = execute or FakeExecute()
        self.default_admin = ADMIN
        self.assessor = False

    def defaultAdmin(self):
        return FakeCall(self.default_admin)

    def isAssessor(self, _address):
        return FakeCall(self.assessor)

    def execute(self, _request):
        return self.execute_call


class FakeAccount:
    """Sign deterministic transaction dictionaries without a private key."""

    address = RELAYER

    def sign_transaction(self, _transaction):
        return SimpleNamespace(
            raw_transaction=HexBytes("0x01"),
            hash=HexBytes(TRANSACTION_HASH),
        )


def gateway(functions: FakeGatewayFunctions | None = None) -> GaslessClaimsGateway:
    """Create a network-free gateway around configurable contract functions."""

    adapter = GaslessClaimsGateway.__new__(GaslessClaimsGateway)
    adapter.deployment = DEPLOYMENT
    adapter.forward_gas = 250_000
    adapter.signature_ttl_seconds = 600
    adapter.registry = FakeRegistry(functions)
    adapter.forwarder = SimpleNamespace(functions=adapter.registry.functions)
    adapter.w3 = SimpleNamespace(
        eth=SimpleNamespace(get_block=lambda _block: {"timestamp": 1_000})
    )
    return adapter


def relay_chain(
    *,
    execute: FakeExecute | None = None,
    base_fee: int = 10,
    priority_fee: int = 2,
) -> GaslessRelayChain:
    """Create a network-free relay adapter with deterministic fee data."""

    adapter = GaslessRelayChain.__new__(GaslessRelayChain)
    adapter.deployment = DEPLOYMENT
    adapter.forward_gas = 250_000
    adapter.signature_ttl_seconds = 600
    adapter.max_transaction_gas = 500_000
    adapter.max_fee_per_gas_wei = 100
    adapter.max_priority_fee_per_gas_wei = 10
    adapter.account = FakeAccount()
    functions = FakeRelayFunctions(execute)
    adapter.registry = FakeRegistry(functions)
    adapter.forwarder = SimpleNamespace(functions=functions)
    adapter.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=lambda _block: {"baseFeePerGas": base_fee},
            max_priority_fee=priority_fee,
            gas_price=base_fee,
            get_transaction_count=lambda _address, _state: 9,
            block_number=112,
        )
    )
    return adapter


def test_forward_request_round_trips_durable_values_and_typed_domain():
    request = PreparedForwardRequest.from_record(submission_record())

    assert request.contract_value(SIGNATURE) == {
        "from": SIGNER,
        "to": REGISTRY,
        "value": 0,
        "gas": 250_000,
        "deadline": 2_000_000_000,
        "data": "0x1234",
        "signature": SIGNATURE,
    }
    typed_data = request.typed_data(
        chain_id=DEPLOYMENT.chain_id,
        forwarder_address=FORWARDER,
    )
    assert typed_data["domain"]["verifyingContract"] == FORWARDER
    assert typed_data["message"]["nonce"] == "7"
    assert typed_data["message"]["gas"] == "250000"

    with pytest.raises(GaslessBlockchainError, match="complete forward request"):
        PreparedForwardRequest.from_record(submission_record(call_data=None))


def test_gateway_prepares_allowlisted_call_and_verifies_signature():
    functions = FakeGatewayFunctions()
    adapter = gateway(functions)

    request = adapter.prepare_request(
        signer_address=SIGNER,
        claim_hash=HexBytes(CLAIM_HASH),
        data_pointer="ipfs://bafy-test",
    )
    adapter.verify_signature(submission_record(), SIGNATURE)

    assert request.nonce == 7
    assert request.deadline == 1_600
    assert request.to == REGISTRY
    assert adapter.registry.encoded == (
        "submitClaim",
        [HexBytes(CLAIM_HASH), "ipfs://bafy-test"],
    )
    assert functions.verified_request["signature"] == SIGNATURE


def test_gateway_rejects_missing_role_and_invalid_or_unavailable_signature():
    functions = FakeGatewayFunctions()
    adapter = gateway(functions)
    functions.submitter = False
    with pytest.raises(GaslessBlockchainError, match="does not hold SUBMITTER_ROLE"):
        adapter.validate_signer(SIGNER)

    functions.submitter = True
    functions.signature_valid = False
    with pytest.raises(GaslessBlockchainError, match="invalid, expired"):
        adapter.verify_signature(submission_record(), SIGNATURE)

    functions.verify_error = RuntimeError("rpc unavailable")
    with pytest.raises(GaslessBlockchainError, match="Could not verify"):
        adapter.verify_signature(submission_record(), SIGNATURE)


def test_gateway_configuration_builds_validated_adapters_and_enforces_caps(
    monkeypatch,
):
    registry = FakeRegistry()
    forwarder = SimpleNamespace(functions=FakeGatewayFunctions())
    monkeypatch.setattr(gasless, "load_claims_deployment", lambda _settings: DEPLOYMENT)
    monkeypatch.setattr(
        gasless, "connect_claims_deployment", lambda _w3, _deployment: registry
    )
    monkeypatch.setattr(
        gasless, "connect_claims_forwarder", lambda _w3, _deployment: forwarder
    )

    adapter = GaslessClaimsGateway.from_mapping({"RPC_URL": "http://rpc.test"})

    assert adapter.deployment == DEPLOYMENT
    assert adapter.forward_gas == 250_000
    with pytest.raises(GaslessBlockchainError, match="SEPOLIA_RPC_URL is required"):
        GaslessClaimsGateway.from_mapping({})
    with pytest.raises(GaslessBlockchainError, match="sponsorship cap"):
        GaslessClaimsGateway.from_mapping(
            {"RPC_URL": "http://rpc.test", "GASLESS_FORWARD_GAS": "500001"}
        )
    with pytest.raises(GaslessBlockchainError, match="cannot exceed 3600"):
        GaslessClaimsGateway.from_mapping(
            {
                "RPC_URL": "http://rpc.test",
                "GASLESS_SIGNATURE_TTL_SECONDS": "3601",
            }
        )
    with pytest.raises(GaslessBlockchainError, match="positive integer"):
        GaslessClaimsGateway.from_mapping(
            {"RPC_URL": "http://rpc.test", "GASLESS_FORWARD_GAS": "zero"}
        )


def test_gateway_translates_deployment_configuration_failure(monkeypatch):
    def fail(_settings):
        raise DeploymentConfigurationError("artifact missing")

    monkeypatch.setattr(gasless, "load_claims_deployment", fail)
    with pytest.raises(GaslessBlockchainError, match="artifact missing"):
        GaslessClaimsGateway.from_mapping({"RPC_URL": "http://rpc.test"})


def test_relay_quotes_normal_and_replacement_fees_with_hard_caps():
    adapter = relay_chain()

    assert adapter.pending_nonce() == 9
    assert adapter._fee_quote() == (22, 2)
    assert adapter._fee_quote(
        minimum_max_fee_per_gas=40,
        minimum_priority_fee_per_gas=4,
    ) == (45, 5)

    adapter.max_fee_per_gas_wei = 20
    with pytest.raises(GaslessBlockchainError, match="sponsorship cap"):
        adapter._fee_quote(minimum_max_fee_per_gas=30)


def test_relay_signer_builds_only_the_prevalidated_forwarder_transaction():
    execute = FakeExecute(estimate=100_000)
    adapter = relay_chain(execute=execute)

    signed = adapter.sign_relay(submission_record(), relayer_nonce=9)

    assert signed.nonce == 9
    assert signed.raw_transaction == "0x01"
    assert signed.transaction_hash == TRANSACTION_HASH
    assert execute.built == {
        "from": RELAYER,
        "nonce": 9,
        "chainId": DEPLOYMENT.chain_id,
        "value": 0,
        "gas": 120_000,
        "maxFeePerGas": 22,
        "maxPriorityFeePerGas": 2,
    }


def test_relay_signer_rejects_incomplete_wrong_target_and_excess_gas():
    adapter = relay_chain()
    with pytest.raises(GaslessBlockchainError, match="no insurer signature"):
        adapter.prepare_relay_signer(submission_record(insurer_signature=None))
    with pytest.raises(GaslessBlockchainError, match="does not match"):
        adapter.prepare_relay_signer(submission_record(chain_id=1))

    adapter = relay_chain(execute=FakeExecute(estimate=500_000))
    with pytest.raises(GaslessBlockchainError, match="gas cap"):
        adapter.prepare_relay_signer(submission_record())


@pytest.mark.parametrize(
    ("rpc_error", "receipt", "expected_error"),
    [
        (ValueError("already known"), None, None),
        (ValueError("nonce too low"), {"status": 1}, None),
        (
            ValueError("nonce too low"),
            None,
            "nonce was consumed by an unknown transaction",
        ),
        (ValueError("replacement underpriced"), None, "RPC rejected"),
    ],
)
def test_broadcast_classifies_idempotent_and_unsafe_rpc_responses(
    rpc_error,
    receipt,
    expected_error,
):
    adapter = relay_chain()
    adapter.w3.eth.send_raw_transaction = lambda _raw: (_ for _ in ()).throw(rpc_error)
    adapter.receipt = lambda _hash: receipt

    if expected_error is None:
        assert adapter.broadcast("0x01", TRANSACTION_HASH) == TRANSACTION_HASH
    else:
        with pytest.raises(GaslessBlockchainError, match=expected_error):
            adapter.broadcast("0x01", TRANSACTION_HASH)


def test_broadcast_receipt_and_confirmation_depth_are_verified():
    adapter = relay_chain()
    adapter.w3.eth.send_raw_transaction = lambda _raw: HexBytes(TRANSACTION_HASH)
    assert adapter.broadcast("0x01", TRANSACTION_HASH) == TRANSACTION_HASH

    adapter.w3.eth.get_transaction_receipt = lambda _hash: {"blockNumber": 100}
    receipt = adapter.receipt(TRANSACTION_HASH)
    assert receipt == {"blockNumber": 100}
    assert adapter.has_confirmations(receipt, 12) is True
    adapter.w3.eth.block_number = 111
    assert adapter.has_confirmations(receipt, 12) is False


def test_confirm_requires_one_matching_claim_submitted_event():
    event = {
        "args": {
            "claimant": SIGNER,
            "claimHash": HexBytes(CLAIM_HASH),
            "dataPointer": "ipfs://bafy-test",
            "claimId": 7,
        }
    }
    processor = SimpleNamespace(process_receipt=lambda _receipt: [event])
    claim_submitted = lambda: processor
    adapter = relay_chain()
    adapter.registry.events = SimpleNamespace(ClaimSubmitted=claim_submitted)
    receipt = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": HexBytes(TRANSACTION_HASH),
    }

    result = adapter.confirm(submission_record(), receipt)

    assert result.claim_id == 7
    assert result.transaction_hash == TRANSACTION_HASH
    assert result.block_number == 100
    with pytest.raises(GaslessBlockchainError, match="reverted"):
        adapter.confirm(submission_record(), {**receipt, "status": 0})
    adapter.registry.events = SimpleNamespace(
        ClaimSubmitted=lambda: SimpleNamespace(process_receipt=lambda _receipt: [])
    )
    with pytest.raises(GaslessBlockchainError, match="exactly one"):
        adapter.confirm(submission_record(), receipt)


def test_confirm_rejects_event_that_does_not_match_authorization():
    event = {
        "args": {
            "claimant": RELAYER,
            "claimHash": HexBytes(CLAIM_HASH),
            "dataPointer": "ipfs://bafy-test",
            "claimId": 7,
        }
    }
    adapter = relay_chain()
    adapter.registry.events = SimpleNamespace(
        ClaimSubmitted=lambda: SimpleNamespace(process_receipt=lambda _receipt: [event])
    )
    with pytest.raises(GaslessBlockchainError, match="does not match"):
        adapter.confirm(
            submission_record(),
            {
                "status": 1,
                "blockNumber": 100,
                "transactionHash": HexBytes(TRANSACTION_HASH),
            },
        )
