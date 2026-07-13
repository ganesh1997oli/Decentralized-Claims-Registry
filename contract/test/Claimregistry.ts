import assert from "node:assert/strict";
import {describe, it} from "node:test";

import {network} from "hardhat";
import {keccak256, toHex, getAddress} from "viem";

// The *WithArgs matchers accept a `(value) => boolean` predicate at any arg
// position. `anyArg` matches anything used here for the block timestamp,
// whose exact value we don't want to pin down.

const anyArg = () => true;

describe("ClaimsRegistry", async function() {
    const { viem } = await network.create();

    // Enum Status in the contract: Submitted = 0, UnderReview=1, Approved=2,
    // Rejected=3, Flagged=4. Note: viem decodes small Solidity integers
    // (uint8/uint16, so enums and fraudscore) as JS `number`, but uint256
    // (claimId, timestamp) as `bigint`. The values below reflext that.

    const Status = {
        Submitted: 0,
        UnderReview: 1,
        Approved: 2,
        Rejected: 3,
        Flagged: 4
    } as const;

    // Deterministic sample claim data. In the real flow, claimHash is the 
    // keccak256 of the canonical off-chain payload and dataPointer is where it lives.
    const claimHash = keccak256(toHex("policy-42:incident-2026-07-13"));
    const dataPointer = "ipfs://bafybeigdyrzexamplecidexamplecidexampleci";

    it("records a claim and emits ClaimSubmitted with indexed claimId + claimant", async function () {
        const registry = await viem.deployContract("ClaimsRegistry");
        const [, claimant] = await viem.getWalletClients();

        await viem.assertions.emitWithArgs(
            registry.write.submitClaim([claimHash, dataPointer], {
                account: claimant.account,
            }),
            registry,
            "ClaimSubmitted",
            [0n, getAddress(claimant.account.address), claimHash, dataPointer, anyArg],
        );

        assert.equal(await registry.read.claimCount(), 1n);
    })
})