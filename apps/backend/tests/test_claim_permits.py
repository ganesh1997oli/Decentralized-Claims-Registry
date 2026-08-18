import json

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.backend.app.claim_permits import (
    CLAIM_PERMIT_DOMAIN_NAME,
    CLAIM_PERMIT_DOMAIN_VERSION,
    CLAIM_PERMIT_FIELDS,
    ClaimPermit,
    ClaimPermitConfigurationError,
    FileClaimPermitIssuer,
)

REGISTRY = "0x1111111111111111111111111111111111111111"
INSURER = "0x2222222222222222222222222222222222222222"
CLAIMANT = "0x3333333333333333333333333333333333333333"


def permit() -> ClaimPermit:
    return ClaimPermit(
        claimant=CLAIMANT,
        submitter=CLAIMANT,
        insurer=INSURER,
        claimant_commitment="0x" + ("11" * 32),
        claim_hash="0x" + ("22" * 32),
        data_pointer_hash="0x" + ("33" * 32),
        permit_id="0x" + ("44" * 32),
        deadline=2_000_000_000,
    )


def test_file_permit_issuer_signs_the_exact_eip712_message(tmp_path):
    account = Account.create("permit-issuer")
    key_file = tmp_path / "permit-issuer.key"
    key_file.write_text(account.key.hex(), encoding="utf-8")
    key_file.chmod(0o600)
    issuer = FileClaimPermitIssuer.from_mapping(
        {
            "CLAIM_PERMIT_ISSUERS_JSON": json.dumps(
                [
                    {
                        "insurerId": "northstar-mutual",
                        "privateKeyFile": str(key_file),
                    }
                ]
            )
        },
        chain_id=11_155_111,
        registry_address=REGISTRY,
    )

    signed = issuer.issue("northstar-mutual", permit())
    signable = encode_typed_data(
        domain_data={
            "name": CLAIM_PERMIT_DOMAIN_NAME,
            "version": CLAIM_PERMIT_DOMAIN_VERSION,
            "chainId": 11_155_111,
            "verifyingContract": REGISTRY,
        },
        message_types={"ClaimPermit": list(CLAIM_PERMIT_FIELDS)},
        message_data=permit().message(),
    )

    assert signed.issuer_address == account.address
    assert Account.recover_message(signable, signature=signed.signature) == account.address


def test_file_permit_issuer_rejects_group_readable_private_keys(tmp_path):
    account = Account.create("insecure-permit-issuer")
    key_file = tmp_path / "permit-issuer.key"
    key_file.write_text(account.key.hex(), encoding="utf-8")
    key_file.chmod(0o640)

    with pytest.raises(ClaimPermitConfigurationError, match="owner-only"):
        FileClaimPermitIssuer.from_mapping(
            {
                "CLAIM_PERMIT_ISSUERS_JSON": json.dumps(
                    [
                        {
                            "insurerId": "northstar-mutual",
                            "privateKeyFile": str(key_file),
                        }
                    ]
                )
            },
            chain_id=11_155_111,
            registry_address=REGISTRY,
        )
