import json
from types import SimpleNamespace

import pytest

from packages.integrations.ethereum import (
    CLAIMS_FORWARDER_MODULE_ID,
    CLAIMS_REGISTRY_MODULE_ID,
    DEFAULT_DEPLOYMENTS_ROOT,
    DeploymentConfigurationError,
    DeploymentValidationError,
    connect_claims_deployment,
    load_claims_deployment,
)
from packages.integrations.ethereum.deployment import (
    FORWARDER_REQUIRED_FUNCTIONS,
    GASLESS_REGISTRY_FUNCTIONS,
    REQUIRED_EVENTS,
    REQUIRED_FUNCTIONS,
)

HARDENED_DEPLOYMENT_ID = "sepolia-security-audit-v1"
HARDENED_ADDRESS = "0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB"


def test_hardened_checked_in_deployment_loads_with_expected_address():
    deployment = load_claims_deployment(
        {"CLAIMS_DEPLOYMENT_ID": HARDENED_DEPLOYMENT_ID}
    )

    assert deployment.deployment_id == HARDENED_DEPLOYMENT_ID
    assert deployment.chain_id == 11_155_111
    assert deployment.address == HARDENED_ADDRESS
    assert any(
        entry.get("type") == "function" and entry.get("name") == "isSubmitter"
        for entry in deployment.abi
    )


def test_deployment_id_is_required():
    with pytest.raises(DeploymentConfigurationError, match="required"):
        load_claims_deployment({})


@pytest.mark.parametrize(
    "deployment_id",
    ("../sepolia-security-audit-v1", "nested/deployment", "nested\\deployment"),
)
def test_deployment_id_cannot_escape_deployments_root(deployment_id):
    with pytest.raises(DeploymentConfigurationError, match="safe"):
        load_claims_deployment({"CLAIMS_DEPLOYMENT_ID": deployment_id})


def test_legacy_deployment_is_rejected_for_missing_hardened_interface():
    with pytest.raises(DeploymentConfigurationError, match="incompatible"):
        load_claims_deployment({"CLAIMS_DEPLOYMENT_ID": "chain-11155111"})


def test_malformed_artifact_has_a_configuration_error(tmp_path):
    deployment_dir = tmp_path / "broken"
    artifact_dir = deployment_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (deployment_dir / "deployed_addresses.json").write_text(
        json.dumps(
            {
                CLAIMS_REGISTRY_MODULE_ID: (
                    "0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB"
                )
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / f"{CLAIMS_REGISTRY_MODULE_ID}.json").write_text(
        "not-json", encoding="utf-8"
    )

    with pytest.raises(DeploymentConfigurationError, match="Could not load"):
        load_claims_deployment(
            {"CLAIMS_DEPLOYMENT_ID": "broken"}, deployments_root=tmp_path
        )


def test_gasless_deployment_loads_registry_and_forwarder_as_one_identity(tmp_path):
    deployment_dir = tmp_path / "gasless-v2"
    artifact_dir = deployment_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    registry_address = "0x1111111111111111111111111111111111111111"
    forwarder_address = "0x2222222222222222222222222222222222222222"
    (deployment_dir / "deployed_addresses.json").write_text(
        json.dumps(
            {
                CLAIMS_REGISTRY_MODULE_ID: registry_address,
                CLAIMS_FORWARDER_MODULE_ID: forwarder_address,
            }
        ),
        encoding="utf-8",
    )
    registry_abi = [
        *({"type": "function", "name": name} for name in REQUIRED_FUNCTIONS),
        *(
            {"type": "function", "name": name}
            for name in GASLESS_REGISTRY_FUNCTIONS
        ),
        *({"type": "event", "name": name} for name in REQUIRED_EVENTS),
    ]
    forwarder_abi = [
        {"type": "function", "name": name}
        for name in FORWARDER_REQUIRED_FUNCTIONS
    ]
    (artifact_dir / f"{CLAIMS_REGISTRY_MODULE_ID}.json").write_text(
        json.dumps({"abi": registry_abi}),
        encoding="utf-8",
    )
    (artifact_dir / f"{CLAIMS_FORWARDER_MODULE_ID}.json").write_text(
        json.dumps({"abi": forwarder_abi}),
        encoding="utf-8",
    )

    deployment = load_claims_deployment(
        {"CLAIMS_DEPLOYMENT_ID": "gasless-v2"}, deployments_root=tmp_path
    )

    assert deployment.supports_gasless is True
    assert deployment.address == registry_address
    assert deployment.forwarder_address == forwarder_address


class FakeCall:
    def __init__(self, *, error=None):
        self.error = error

    def call(self):
        if self.error:
            raise self.error
        return "0x0000000000000000000000000000000000000001"


class FakeWeb3:
    def __init__(self, *, chain_id=11_155_111, code=b"\x01", call_error=None):
        contract = SimpleNamespace(
            functions=SimpleNamespace(
                defaultAdmin=lambda: FakeCall(error=call_error)
            )
        )
        self.eth = SimpleNamespace(
            chain_id=chain_id,
            get_code=lambda _address: code,
            contract=lambda **_kwargs: contract,
        )

    @staticmethod
    def is_connected():
        return True


def _deployment():
    return load_claims_deployment(
        {"CLAIMS_DEPLOYMENT_ID": HARDENED_DEPLOYMENT_ID},
        deployments_root=DEFAULT_DEPLOYMENTS_ROOT,
    )


def test_live_connection_rejects_wrong_network():
    with pytest.raises(DeploymentValidationError, match="RPC returned"):
        connect_claims_deployment(FakeWeb3(chain_id=1), _deployment())


def test_live_connection_rejects_address_without_bytecode():
    with pytest.raises(DeploymentValidationError, match="No contract bytecode"):
        connect_claims_deployment(FakeWeb3(code=b""), _deployment())


def test_live_connection_rejects_incompatible_address():
    with pytest.raises(DeploymentValidationError, match="hardened"):
        connect_claims_deployment(
            FakeWeb3(call_error=RuntimeError("reverted")), _deployment()
        )
