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
    });

    it("stores the claim with Submitted status and the submitter as claimant", async function(){
        const registry = await viem.deployContract("ClaimsRegistry");
        const [, claimant] = await viem.getWalletClients();
        await registry.write.submitClaim([claimHash, dataPointer], {
            account: claimant.account,
        });

        const claim = await registry.read.getClaim([0n]);
        assert.equal(getAddress(claim[0]), getAddress(claimant.account.address));
        assert.equal(claim[1], claimHash);
        assert.equal(claim[2], dataPointer);
        assert.equal(claim[3], Status.Submitted);
        assert.equal(claim[4], 0); // fraudScore starts at 0
    });

    it("rejects an empty claim hash", async function() {
        const registry = await viem.deployContract("ClaimRegistry");
        const zeroHash = "0x0000000000000000000000000000000000000000000000000000000000000000" as const;

        await viem.assertions.revertWithCustomError(
            registry.write.submitClaim([zeroHash, dataPointer]),
            registry,
            "EmptyClaimHash"
        );
    });

    it("lets an authorized assessor write an assessment back and emits ClaimAssessed", async function() {
        const registry = await viem.deployContract("ClaimsRegistry");
        const [owner, claimant, assessor] = await viem.getWalletClients();

        await registry.write.submitClaim([claimHash, dataPointer], {
            account: claimant.account,
        });

        // owner (deployer) authorizes the assessor
        await registry.write.setAssessor([assessor.account.address, true], {
            account: owner.account,
        });

        await viem.assertions.emitWithArgs(
            registry.write.assessClaim([0n, Status.Flagged, 8500], {
                account: assessor.account,
            }),
            registry,
            "ClaimAssessed",
            [0n, Status.Flagged, getAddress(assessor.account.address), 8500, anyArg],
        );

        const claim = await registry.read.getClaim([0n]);
        assert.equal(claim[3], Status.Flagged);
        assert.equal(claim[4], 8500);
    });

    it("blocks assessment from a non-assessor", async function() {
        const registry = await viem.deployContract("ClaimsRegistry");
        const [, claimant, notAssessor] = await viem.getWalletClients();

        await registry.write.submitClaim([claimHash, dataPointer], {
            account: claimant.account,
        });

        await viem.assertions.revertWithCustomError(
            registry.write.assessClaim([0n, Status.Approved, 0], {
                account: notAssessor.account,
            }),
            registry,
            "NotAssessor"
        );
    });

    it("blocks setAddressor from a non-owner", async function() {
        const registry = await viem.deployContract("ClaimsRegistry");
        const [, alice, bob] = await viem.getWalletClients();

        await viem.assertions.revertWithCustomError(
            registry.write.setAssessor([bob.account.address, true], {
                account: alice.account,
            }),
            registry,
            "NotOwner"
        );
    });

    it("treats Approved/Rejected claims as final", async function (){
        const registry = await viem.deployContract("ClaimsRegistry");
        const [owner, claimant, assessor] = await viem.getWalletClients();

        await registry.write.submitClaim([claimHash, dataPointer], {
            account: claimant.account,
        });

        await registry.write.setAssessor([assessor.account.address, true], {
            account: owner.account
        });

        await registry.write.assessClaim([0n, Status.Approved, 100], {
            account: assessor.account,
        });

        await viem.assertions.revertWithCustomError(
            registry.write.assessClaim([0n, Status.Rejected, 200], {
                account: assessor.account
            }),
            registry,
            "ClaimAlreadyFinalized",
        );
    });

    it("rejects an out-of-range fraud score", async function() {
        const registry = await viem.deployContract("ClaimsRegistry");
        const [owner, claimant, assessor] = await viem.getWalletClients();

        await registry.write.submitClaim([claimHash, dataPointer], {
            account: claimant.account,
        });
        await registry.write.setAssessor([assessor.account.address, true], {
            account: owner.account,
        });

        await viem.assertions.revertWithCustomError(
            registry.write.assessClaim([0n, Status.Flagged, 10001], {
                account: assessor.account,
            }),
            registry,
            "InvalidFraudScore"
        );
    });
})