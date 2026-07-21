import threading
from types import SimpleNamespace

from web3.exceptions import Web3RPCError

from backend.app.blockchain import SepoliaClaimsRegistry


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
