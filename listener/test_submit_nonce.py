"""Regression tests for sequential transaction nonce allocation."""

import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from web3.exceptions import Web3RPCError


SCRIPT = Path(__file__).with_name("submit_and_assess_demo.py")


def load_send_function(w3, acct):
    """Load only send() from the script, avoiding its live Sepolia entrypoint."""
    tree = ast.parse(SCRIPT.read_text(), filename=str(SCRIPT))
    send_node = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "send"
    )
    namespace = {
        "w3": w3,
        "acct": acct,
        "next_nonce": None,
        "re": re,
        "Web3RPCError": Web3RPCError,
    }
    exec(
        compile(ast.Module(body=[send_node], type_ignores=[]), str(SCRIPT), "exec"),
        namespace,
    )
    return namespace["send"]


class FakeFunction:
    def __init__(self, built_nonces):
        self.built_nonces = built_nonces

    def build_transaction(self, transaction):
        self.built_nonces.append(transaction["nonce"])
        return transaction


class FakeAccount:
    address = "0x0000000000000000000000000000000000000001"

    @staticmethod
    def sign_transaction(transaction):
        return SimpleNamespace(raw_transaction=transaction)


class FakeEth:
    chain_id = 11155111

    def __init__(self):
        self.sent = 0

    @staticmethod
    def get_transaction_count(_address, _block_identifier="latest"):
        # Reproduce the stale public-RPC response seen by the user.
        return 6

    def send_raw_transaction(self, _raw_transaction):
        self.sent += 1
        return bytes([self.sent])

    @staticmethod
    def wait_for_transaction_receipt(_tx_hash):
        return {"status": 1}


class StaleFirstNonceEth(FakeEth):
    def send_raw_transaction(self, raw_transaction):
        if raw_transaction["nonce"] == 6:
            raise Web3RPCError(
                {
                    "code": -32000,
                    "message": "nonce too low: next nonce 7, tx nonce 6",
                }
            )
        return super().send_raw_transaction(raw_transaction)


class SequentialNonceTests(unittest.TestCase):
    def test_two_transactions_increment_nonce_locally(self):
        built_nonces = []
        send = load_send_function(SimpleNamespace(eth=FakeEth()), FakeAccount())

        send(FakeFunction(built_nonces))
        send(FakeFunction(built_nonces))

        self.assertEqual(built_nonces, [6, 7])

    def test_retries_from_nonce_reported_by_rpc(self):
        built_nonces = []
        send = load_send_function(
            SimpleNamespace(eth=StaleFirstNonceEth()), FakeAccount()
        )

        send(FakeFunction(built_nonces))

        self.assertEqual(built_nonces, [6, 7])


if __name__ == "__main__":
    unittest.main()
