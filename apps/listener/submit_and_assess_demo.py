"""Drive the full IPFS-backed claim loop from Python.

Exercise both least-privilege roles for demonstration:

1. submitter: upload a synthetic claim to IPFS, then submitClaim(hash, pointer)
2. assessor: assessClaim(claimId, status, fraudScore) <- the write-back

The two transactions are deliberately signed by different role accounts.

Env vars:
SEPOLIA_SUBMITTER_PRIVATE_KEY required when submitting a new claim.
SEPOLIA_ASSESSOR_PRIVATE_KEY required for assessment.
PINATA_JWT required. Pinata token with public Files write access.
CLAIM_AUTHORIZATION_KEY required to create the worker-verifiable claim document.
SEPOLIA_RPC_URL required for the selected Sepolia deployment.
IPFS_GATEWAY defaults to https://gateway.pinata.cloud/ipfs.
CLAIMS_DEPLOYMENT_ID selects the checked-in Ignition deployment.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from web3 import Web3
from web3.exceptions import Web3RPCError

if not __package__:
    # Keep direct execution working while the shared IPFS module lives at the
    # repository root.
    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from apps.backend.app.models import ClaimSubmission
from apps.backend.app.submission_auth import (
    SUBMIT_CLAIM_OPERATION,
    ClaimAuthorizationSigner,
    InsurerPrincipal,
)
from packages.integrations.ethereum import (
    DeploymentConfigurationError,
    DeploymentValidationError,
    connect_claims_deployment,
    load_claims_deployment,
)
from packages.integrations.ipfs import IPFSClient, IPFSError

RPC_URL = os.environ.get("SEPOLIA_RPC_URL") or os.environ.get("RPC_URL")
if not RPC_URL:
    raise SystemExit("SEPOLIA_RPC_URL is required")

STATUS_NAMES = ["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"]
FLAGGED = 4  # Status.Flagged

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--assess-existing",
    type=int,
    metavar="CLAIM_ID",
    help="skip IPFS upload/submission and assess an existing Submitted claim",
)
args = parser.parse_args()


w3 = Web3(Web3.HTTPProvider(RPC_URL))
try:
    deployment = load_claims_deployment(os.environ)
    contract = connect_claims_deployment(w3, deployment)
except (DeploymentConfigurationError, DeploymentValidationError) as exc:
    raise SystemExit(str(exc)) from exc
CONTRACT_ADDRESS = deployment.address

assessor_private_key = os.environ.get("SEPOLIA_ASSESSOR_PRIVATE_KEY")
if not assessor_private_key:
    raise SystemExit(
        "SEPOLIA_ASSESSOR_PRIVATE_KEY is required to sign the assessment"
    )
assessor_account = w3.eth.account.from_key(assessor_private_key)
assessor_authorized = contract.functions.isAssessor(
    assessor_account.address
).call()
assessor_insurer = contract.functions.assessorInsurer(
    assessor_account.address
).call()
if not assessor_authorized or not contract.functions.isSubmitter(
    assessor_insurer
).call():
    raise SystemExit(
        "The assessor wallet lacks an active assessor-to-submitter scope on the "
        "selected deployment"
    )

submitter_account = None
if args.assess_existing is None:
    submitter_private_key = os.environ.get("SEPOLIA_SUBMITTER_PRIVATE_KEY")
    if not submitter_private_key:
        raise SystemExit(
            "SEPOLIA_SUBMITTER_PRIVATE_KEY is required to submit a new claim"
        )
    submitter_account = w3.eth.account.from_key(submitter_private_key)
    if not contract.functions.isSubmitter(submitter_account.address).call():
        raise SystemExit(
            "The submitter wallet is not authorized on the selected deployment"
        )

print(
    f"Deployment {deployment.deployment_id}: contract {CONTRACT_ADDRESS} "
    f"on chain {deployment.chain_id} via {RPC_URL}"
)
if submitter_account is not None:
    print(f"Submitter account: {submitter_account.address}")
print(f"Assessor account: {assessor_account.address}")

next_nonces: dict[str, int] = {}


def send(fn, account):
    """Sign one contract call, send it, and wait for its receipt."""

    # Public RPC services can briefly return an old transaction count. Read the
    # pending nonce once per role, then keep track of each account independently.
    if account.address not in next_nonces:
        next_nonces[account.address] = w3.eth.get_transaction_count(
            account.address, "pending"
        )

    for attempt in range(2):
        nonce = next_nonces[account.address]
        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            }
        )
        signed = account.sign_transaction(tx)
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        except Web3RPCError as exc:
            match = re.search(r"next nonce\s+(\d+)", str(exc), re.IGNORECASE)
            if attempt == 0 and match and int(match.group(1)) > nonce:
                next_nonces[account.address] = int(match.group(1))
                print(
                    f"RPC nonce was stale ({nonce}); retrying with "
                    f"nonce {next_nonces[account.address]} ..."
                )
                continue
            raise
        next_nonces[account.address] = nonce + 1
        break

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise SystemExit(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

# Step 1: upload and submit a new claim, or continue an existing claim.
if args.assess_existing is None:
    claim_id = contract.functions.claimCount().call()  # next id == current count
    claim = ClaimSubmission.model_validate(
        {
        "insurerId": "northstar-mutual",
        "claimReference": f"synthetic-claim-{claim_id}",
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
        "description": "Synthetic bumper damage claim for IPFS integration testing",
        "evidence": [],
        }
    )
    demo_principal = InsurerPrincipal(
        insurer_id=claim.insurer_id,
        credential_id=os.environ.get(
            "DEMO_INSURER_CREDENTIAL_ID", "northstar-local-v1"
        ),
        permitted_operations=frozenset({SUBMIT_CLAIM_OPERATION}),
        daily_quota=1,
    )
    payload = ClaimAuthorizationSigner.from_env().authorized_claim_bytes(
        claim,
        demo_principal,
    )

    try:
        ipfs = IPFSClient.from_env(require_upload=True)
        cid = ipfs.upload_bytes(
            payload,
            filename=f"claim-{claim_id}.json",
            content_type="application/json",
        )
        data_pointer = f"ipfs://{cid}"
        downloaded_payload = ipfs.download_pointer(data_pointer)
    except IPFSError as exc:
        raise SystemExit(f"IPFS setup failed: {exc}") from exc

    if downloaded_payload != payload:
        raise SystemExit(
            "IPFS round-trip verification failed before blockchain submission"
        )

    claim_hash = Web3.keccak(payload)
    print(f"Uploaded synthetic claim to {data_pointer}")
    print(f"Gateway URL: {ipfs.gateway_url(data_pointer)}")
    print(f"IPFS round-trip: PASSED ({len(payload)} bytes)")

    print(f"Submitting claim #{claim_id} ...")
    r1 = send(
        contract.functions.submitClaim(claim_hash, data_pointer),
        submitter_account,
    )
    print(f" mined in block {r1['blockNumber']}")

    # Ask the contract to confirm that these exact bytes produce its saved hash.
    ok = contract.functions.verifyClaimData(claim_id, payload).call()
    print(f"verifyClaimData(#{claim_id}) -> {ok}")
    assert ok, "IPFS payload hash does not match the on-chain claim hash"
else:
    claim_id = args.assess_existing
    if claim_id < 0:
        raise SystemExit("CLAIM_ID must be zero or greater")
    existing_claim = contract.functions.getClaim(claim_id).call()
    current_status = existing_claim[3]
    if current_status != 0:
        raise SystemExit(
            f"Claim #{claim_id} is already {STATUS_NAMES[current_status]}; "
            "refusing to assess it twice"
        )
    print(
        f"Resuming claim #{claim_id} at {existing_claim[2]} "
        "without uploading or submitting another claim"
    )

# Step 2: write a fixed demonstration assessment to the contract.
fraud_score = 8500  # basis points = 85.00%
print(f"Assessing claim #{claim_id} as Flagged with score {fraud_score} ...")
r2 = send(
    contract.functions.assessClaim(claim_id, FLAGGED, fraud_score),
    assessor_account,
)
print(f" mined in block {r2['blockNumber']}")

# Step 3: read the claim again to show its final saved state.
claim = contract.functions.getClaim(claim_id).call()
print(
    f"Final state: status={STATUS_NAMES[claim[3]]} "
    f"fraudScore={claim[4]} ({claim[4] / 100.0:.2f}%) "
    f"dataPointer={claim[2]}"
)

print("Done - if claims_listener.py is running, it saw both events.")
