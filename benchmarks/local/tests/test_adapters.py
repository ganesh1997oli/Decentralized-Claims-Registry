"""Test benchmark-only identities, stores, signatures, and cleanup guards."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.backend.app.gasless_blockchain import GaslessBlockchainError
from benchmarks.local.adapters import (
    BENCHMARK_CHAIN_ID,
    BENCHMARK_CONTRACT,
    BENCHMARK_FORWARDER,
    BenchmarkEligibility,
    BenchmarkGaslessChain,
    BenchmarkPayloadStore,
    benchmark_account,
    benchmark_session,
    schema_for_run,
    validate_schema_name,
)
from packages.integrations.postgres import GaslessSubmissionRecord


def test_benchmark_identity_is_deterministic_and_token_scoped() -> None:
    first = benchmark_account("benchmark-valid-token-0001")
    repeated = benchmark_account("benchmark-valid-token-0001")
    second = benchmark_account("benchmark-valid-token-0002")

    assert first.address == repeated.address
    assert first.address != second.address
    assert benchmark_session("benchmark-valid-token-0001").claimant_address == (
        first.address
    )


@pytest.mark.parametrize(
    "token",
    ["production-token", "benchmark-short", "benchmark-invalid token"],
)
def test_benchmark_identity_rejects_tokens_outside_explicit_namespace(
    token: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid benchmark bearer token"):
        benchmark_account(token)


def test_payload_store_round_trip_is_content_addressed() -> None:
    store = BenchmarkPayloadStore()
    payload = b'{"claim":"benchmark"}'

    cid = store.upload_bytes(
        payload,
        filename="claim.json",
        content_type="application/json",
    )

    assert store.download_pointer(f"ipfs://{cid}") == payload


def test_benchmark_chain_verifies_the_exact_eip712_authorization() -> None:
    token = "benchmark-signature-token-0001"
    account = benchmark_account(token)
    session = benchmark_session(token)
    principal = BenchmarkEligibility().verify(
        SimpleNamespace(insurer_id="northstar-mutual"),
        session,
    )
    chain = BenchmarkGaslessChain()
    request = chain.prepare_request(
        principal=principal,
        claim_hash=b"\x12" * 32,
        data_pointer="ipfs://benchmark-payload",
        permit_id="0x" + ("34" * 32),
    )
    record = GaslessSubmissionRecord(
        submission_id=UUID("11111111-1111-4111-8111-111111111111"),
        credential_id=principal.credential_id,
        insurer_id=principal.insurer_id,
        signer_address=account.address,
        chain_id=BENCHMARK_CHAIN_ID,
        contract_address=BENCHMARK_CONTRACT,
        forwarder_address=BENCHMARK_FORWARDER,
        idempotency_key_hash="a" * 64,
        client_fingerprint="b" * 64,
        state="prepared",
        call_data=request.data,
        forwarder_nonce=request.nonce,
        forward_gas=request.gas,
        deadline=request.deadline,
    )
    typed_data = request.typed_data(
        chain_id=record.chain_id,
        forwarder_address=record.forwarder_address,
    )
    signature = Account.sign_message(
        encode_typed_data(full_message=typed_data),
        private_key=account.key,
    ).signature.hex()

    chain.verify_signature(record, signature)

    other = benchmark_account("benchmark-signature-token-0002")
    wrong_signature = Account.sign_message(
        encode_typed_data(full_message=typed_data),
        private_key=other.key,
    ).signature.hex()
    with pytest.raises(GaslessBlockchainError, match="signature is invalid"):
        chain.verify_signature(record, wrong_signature)


def test_schema_names_are_disposable_and_fail_closed() -> None:
    schema = schema_for_run("HTTP run/1")

    assert schema.startswith("claims_bench_http_run_1_")
    assert validate_schema_name(schema) == schema
    with pytest.raises(ValueError, match="Benchmark schema"):
        validate_schema_name("public; DROP SCHEMA public CASCADE")
