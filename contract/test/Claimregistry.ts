import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { network } from "hardhat";
import { getAddress, keccak256, toHex } from "viem";

const anyArg = () => true;

describe("ClaimsRegistry", async function () {
  const { viem } = await network.create();
  const Status = {
    Submitted: 0,
    UnderReview: 1,
    Approved: 2,
    Rejected: 3,
    Flagged: 4,
  } as const;
  const claimHash = keccak256(toHex("policy-42:incident-2026-07-13"));
  const dataPointer = "ipfs://bafybeigdyrzexamplecidexamplecidexampleci";

  async function deployFixture() {
    const [admin, insurer, assessor, otherInsurer, otherAssessor] =
      await viem.getWalletClients();
    const registry = await viem.deployContract("ClaimsRegistry", [
      admin.account.address,
      insurer.account.address,
      assessor.account.address,
      0,
    ]);
    return {
      registry,
      admin,
      insurer,
      assessor,
      otherInsurer,
      otherAssessor,
    };
  }

  it("records a claim from an authorized insurer", async function () {
    const { registry, insurer } = await deployFixture();

    await viem.assertions.emitWithArgs(
      registry.write.submitClaim([claimHash, dataPointer], {
        account: insurer.account,
      }),
      registry,
      "ClaimSubmitted",
      [
        0n,
        getAddress(insurer.account.address),
        claimHash,
        dataPointer,
        anyArg,
      ],
    );

    const claim = await registry.read.getClaim([0n]);
    assert.equal(claim[0], getAddress(insurer.account.address));
    assert.equal(claim[1], claimHash);
    assert.equal(claim[2], dataPointer);
    assert.equal(claim[3], Status.Submitted);
    assert.equal(claim[4], 0);
    assert.equal(await registry.read.claimCount(), 1n);
  });

  it("rejects unauthorized submitters and malformed pointers", async function () {
    const { registry, insurer, otherInsurer } = await deployFixture();

    await viem.assertions.revertWithCustomError(
      registry.write.submitClaim([claimHash, dataPointer], {
        account: otherInsurer.account,
      }),
      registry,
      "AccessControlUnauthorizedAccount",
    );
    await viem.assertions.revertWithCustomError(
      registry.write.submitClaim([claimHash, "https://example.test/claim"], {
        account: insurer.account,
      }),
      registry,
      "InvalidDataPointer",
    );
    await viem.assertions.revertWithCustomError(
      registry.write.submitClaim([claimHash, "ipfs://bad/path"], {
        account: insurer.account,
      }),
      registry,
      "InvalidDataPointer",
    );
  });

  it("rejects an empty claim hash", async function () {
    const { registry, insurer } = await deployFixture();
    const zeroHash =
      "0x0000000000000000000000000000000000000000000000000000000000000000" as const;

    await viem.assertions.revertWithCustomError(
      registry.write.submitClaim([zeroHash, dataPointer], {
        account: insurer.account,
      }),
      registry,
      "EmptyClaimHash",
    );
  });

  it("scopes each assessor to one insurer", async function () {
    const {
      registry,
      admin,
      assessor,
      otherInsurer,
      otherAssessor,
    } = await deployFixture();
    await registry.write.setSubmitter([otherInsurer.account.address, true], {
      account: admin.account,
    });
    await registry.write.setAssessor(
      [otherAssessor.account.address, otherInsurer.account.address, true],
      { account: admin.account },
    );
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: otherInsurer.account,
    });

    await viem.assertions.revertWithCustomError(
      registry.write.assessClaim([0n, Status.Flagged, 8500], {
        account: assessor.account,
      }),
      registry,
      "AssessorScopeMismatch",
    );
    await registry.write.assessClaim([0n, Status.Flagged, 8500], {
      account: otherAssessor.account,
    });
    assert.equal((await registry.read.getClaim([0n]))[3], Status.Flagged);
  });

  it("enforces monotonic lifecycle transitions", async function () {
    const { registry, insurer, assessor } = await deployFixture();
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: insurer.account,
    });

    await registry.write.assessClaim([0n, Status.UnderReview, 4200], {
      account: assessor.account,
    });
    await registry.write.assessClaim([0n, Status.Approved, 4200], {
      account: assessor.account,
    });

    await viem.assertions.revertWithCustomError(
      registry.write.assessClaim([0n, Status.Rejected, 4200], {
        account: assessor.account,
      }),
      registry,
      "InvalidStatusTransition",
    );
  });

  it("does not allow the model score to be rewritten", async function () {
    const { registry, insurer, assessor } = await deployFixture();
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: insurer.account,
    });
    await registry.write.assessClaim([0n, Status.Flagged, 8500], {
      account: assessor.account,
    });

    await viem.assertions.revertWithCustomError(
      registry.write.assessClaim([0n, Status.Rejected, 100], {
        account: assessor.account,
      }),
      registry,
      "FraudScoreCannotChange",
    );
  });

  it("requires role-specific admin functions", async function () {
    const { registry, admin, otherInsurer } = await deployFixture();
    const submitterRole = await registry.read.SUBMITTER_ROLE();

    await viem.assertions.revertWithCustomError(
      registry.write.grantRole([submitterRole, otherInsurer.account.address], {
        account: admin.account,
      }),
      registry,
      "UseRoleConfigurationFunction",
    );
  });

  it("prevents privileged role accounts from being reused", async function () {
    const { registry, admin, insurer, otherInsurer } = await deployFixture();

    await viem.assertions.revertWithCustomError(
      registry.write.setSubmitter([admin.account.address, true], {
        account: admin.account,
      }),
      registry,
      "RoleSeparationRequired",
    );
    await viem.assertions.revertWithCustomError(
      registry.write.setAssessor(
        [insurer.account.address, insurer.account.address, true],
        { account: admin.account },
      ),
      registry,
      "RoleSeparationRequired",
    );
    await viem.assertions.revertWithCustomError(
      registry.write.beginDefaultAdminTransfer([insurer.account.address], {
        account: admin.account,
      }),
      registry,
      "RoleSeparationRequired",
    );

    await registry.write.beginDefaultAdminTransfer(
      [otherInsurer.account.address],
      { account: admin.account },
    );
    await registry.write.setSubmitter([otherInsurer.account.address, true], {
      account: admin.account,
    });
    await viem.assertions.revertWithCustomError(
      registry.write.acceptDefaultAdminTransfer({
        account: otherInsurer.account,
      }),
      registry,
      "RoleSeparationRequired",
    );
  });

  it("uses an explicit two-step admin transfer", async function () {
    const { registry, admin, otherInsurer } = await deployFixture();
    await registry.write.beginDefaultAdminTransfer(
      [otherInsurer.account.address],
      { account: admin.account },
    );

    assert.equal(await registry.read.owner(), getAddress(admin.account.address));
    await registry.write.acceptDefaultAdminTransfer({
      account: otherInsurer.account,
    });
    assert.equal(
      await registry.read.owner(),
      getAddress(otherInsurer.account.address),
    );

    await viem.assertions.revertWithCustomError(
      registry.write.setSubmitter([admin.account.address, true], {
        account: admin.account,
      }),
      registry,
      "AccessControlUnauthorizedAccount",
    );
  });
});
