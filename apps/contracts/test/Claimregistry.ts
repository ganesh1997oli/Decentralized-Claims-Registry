import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { network } from "hardhat";
import { encodeFunctionData, getAddress, keccak256, toHex } from "viem";

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
  const claimantCommitment = keccak256(toHex("claimant:northstar:alice"));
  const permitId = keccak256(toHex("public-claim-permit-1"));

  async function deployFixture() {
    const [
      admin,
      insurer,
      permitIssuer,
      assessor,
      otherInsurer,
      otherAssessor,
      decisionMaker,
      otherDecisionMaker,
      claimant,
      representative,
      relayer,
    ] = await viem.getWalletClients();
    const forwarder = await viem.deployContract("ClaimsForwarder");
    const registry = await viem.deployContract("ClaimsRegistry", [
      admin.account.address,
      insurer.account.address,
      permitIssuer.account.address,
      assessor.account.address,
      decisionMaker.account.address,
      forwarder.address,
      0,
    ]);
    return {
      forwarder,
      registry,
      admin,
      insurer,
      permitIssuer,
      assessor,
      otherInsurer,
      otherAssessor,
      decisionMaker,
      otherDecisionMaker,
      claimant,
      representative,
      relayer,
    };
  }

  async function signedClaimPermit(
    overrides: Partial<{
      claimant: `0x${string}`;
      submitter: `0x${string}`;
      insurer: `0x${string}`;
      claimantCommitment: `0x${string}`;
      claimHash: `0x${string}`;
      dataPointerHash: `0x${string}`;
      permitId: `0x${string}`;
      deadline: bigint;
    }> = {},
  ) {
    const fixture = await deployFixture();
    const publicClient = await viem.getPublicClient();
    const chainId = await publicClient.getChainId();
    const latestBlock = await publicClient.getBlock();
    const permit = {
      claimant: fixture.claimant.account.address,
      submitter: fixture.claimant.account.address,
      insurer: fixture.insurer.account.address,
      claimantCommitment,
      claimHash,
      dataPointerHash: keccak256(toHex(dataPointer)),
      permitId,
      deadline: latestBlock.timestamp + 3_600n,
      ...overrides,
    } as const;
    const signature = await fixture.permitIssuer.signTypedData({
      account: fixture.permitIssuer.account,
      domain: {
        name: "ClaimsRegistry",
        version: "2",
        chainId,
        verifyingContract: fixture.registry.address,
      },
      types: {
        ClaimPermit: [
          { name: "claimant", type: "address" },
          { name: "submitter", type: "address" },
          { name: "insurer", type: "address" },
          { name: "claimantCommitment", type: "bytes32" },
          { name: "claimHash", type: "bytes32" },
          { name: "dataPointerHash", type: "bytes32" },
          { name: "permitId", type: "bytes32" },
          { name: "deadline", type: "uint48" },
        ],
      },
      primaryType: "ClaimPermit",
      message: permit,
    });
    return { ...fixture, permit, permitSignature: signature };
  }

  async function signedSubmissionRequest() {
    const { forwarder, registry, insurer, relayer } = await deployFixture();
    const publicClient = await viem.getPublicClient();
    const chainId = await publicClient.getChainId();
    const latestBlock = await publicClient.getBlock();
    const nonce = await forwarder.read.nonces([insurer.account.address]);
    const request = {
      from: insurer.account.address,
      to: registry.address,
      value: 0n,
      gas: 300_000n,
      deadline: latestBlock.timestamp + 3_600n,
      data: encodeFunctionData({
        abi: registry.abi,
        functionName: "submitClaim",
        args: [claimHash, dataPointer],
      }),
    } as const;
    const signature = await insurer.signTypedData({
      account: insurer.account,
      domain: {
        name: "ClaimsRegistryForwarder",
        version: "1",
        chainId,
        verifyingContract: forwarder.address,
      },
      types: {
        ForwardRequest: [
          { name: "from", type: "address" },
          { name: "to", type: "address" },
          { name: "value", type: "uint256" },
          { name: "gas", type: "uint256" },
          { name: "nonce", type: "uint256" },
          { name: "deadline", type: "uint48" },
          { name: "data", type: "bytes" },
        ],
      },
      primaryType: "ForwardRequest",
      message: { ...request, nonce },
    });
    return {
      forwarder,
      registry,
      insurer,
      relayer,
      request: { ...request, signature },
    };
  }

  it("relays a signed claim while preserving the insurer identity", async function () {
    const { forwarder, registry, insurer, relayer, request } =
      await signedSubmissionRequest();

    assert.equal(await forwarder.read.verify([request]), true);
    await forwarder.write.execute([request], { account: relayer.account });

    const claim = await registry.read.getClaim([0n]);
    assert.equal(claim[0], getAddress(insurer.account.address));
    assert.equal(claim[1], claimHash);
    assert.equal(await forwarder.read.nonces([insurer.account.address]), 1n);
    assert.equal(await forwarder.read.verify([request]), false);
  });

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

  it("records a public claimant with an insurer-scoped one-time permit", async function () {
    const {
      registry,
      claimant,
      insurer,
      permitIssuer,
      permit,
      permitSignature,
    } = await signedClaimPermit();

    await registry.write.submitClaimWithPermit(
      [permit, dataPointer, permitSignature],
      { account: claimant.account },
    );

    const claim = await registry.read.getClaim([0n]);
    const parties = await registry.read.getClaimParties([0n]);
    assert.equal(claim[0], getAddress(claimant.account.address));
    assert.equal(claim[1], claimHash);
    assert.equal(parties[0], getAddress(insurer.account.address));
    assert.equal(parties[1], getAddress(claimant.account.address));
    assert.equal(parties[2], claimantCommitment);
    assert.equal(await registry.read.isClaimPermitUsed([permitId]), true);
    assert.equal(
      await registry.read.isPermitIssuerFor([
        permitIssuer.account.address,
        insurer.account.address,
      ]),
      true,
    );
  });

  it("relays a public claimant permit without granting the wallet a role", async function () {
    const {
      forwarder,
      registry,
      claimant,
      relayer,
      permit,
      permitSignature,
    } = await signedClaimPermit();
    const publicClient = await viem.getPublicClient();
    const chainId = await publicClient.getChainId();
    const nonce = await forwarder.read.nonces([claimant.account.address]);
    const request = {
      from: claimant.account.address,
      to: registry.address,
      value: 0n,
      gas: 400_000n,
      deadline: permit.deadline,
      data: encodeFunctionData({
        abi: registry.abi,
        functionName: "submitClaimWithPermit",
        args: [permit, dataPointer, permitSignature],
      }),
    } as const;
    const forwardSignature = await claimant.signTypedData({
      account: claimant.account,
      domain: {
        name: "ClaimsRegistryForwarder",
        version: "1",
        chainId,
        verifyingContract: forwarder.address,
      },
      types: {
        ForwardRequest: [
          { name: "from", type: "address" },
          { name: "to", type: "address" },
          { name: "value", type: "uint256" },
          { name: "gas", type: "uint256" },
          { name: "nonce", type: "uint256" },
          { name: "deadline", type: "uint48" },
          { name: "data", type: "bytes" },
        ],
      },
      primaryType: "ForwardRequest",
      message: { ...request, nonce },
    });

    await forwarder.write.execute(
      [{ ...request, signature: forwardSignature }],
      { account: relayer.account },
    );

    assert.equal(
      (await registry.read.getClaim([0n]))[0],
      getAddress(claimant.account.address),
    );
    assert.equal(await registry.read.isSubmitter([claimant.account.address]), false);
  });

  it("supports an authorized representative without confusing them with the claimant", async function () {
    const fixture = await deployFixture();
    const publicClient = await viem.getPublicClient();
    const chainId = await publicClient.getChainId();
    const latestBlock = await publicClient.getBlock();
    const permit = {
      claimant: fixture.claimant.account.address,
      submitter: fixture.representative.account.address,
      insurer: fixture.insurer.account.address,
      claimantCommitment,
      claimHash,
      dataPointerHash: keccak256(toHex(dataPointer)),
      permitId,
      deadline: latestBlock.timestamp + 3_600n,
    } as const;
    const signature = await fixture.permitIssuer.signTypedData({
      account: fixture.permitIssuer.account,
      domain: {
        name: "ClaimsRegistry",
        version: "2",
        chainId,
        verifyingContract: fixture.registry.address,
      },
      types: {
        ClaimPermit: [
          { name: "claimant", type: "address" },
          { name: "submitter", type: "address" },
          { name: "insurer", type: "address" },
          { name: "claimantCommitment", type: "bytes32" },
          { name: "claimHash", type: "bytes32" },
          { name: "dataPointerHash", type: "bytes32" },
          { name: "permitId", type: "bytes32" },
          { name: "deadline", type: "uint48" },
        ],
      },
      primaryType: "ClaimPermit",
      message: permit,
    });

    await fixture.registry.write.submitClaimWithPermit(
      [permit, dataPointer, signature],
      { account: fixture.representative.account },
    );

    assert.equal(
      (await fixture.registry.read.getClaim([0n]))[0],
      getAddress(fixture.claimant.account.address),
    );
    assert.equal(
      (await fixture.registry.read.getClaimParties([0n]))[1],
      getAddress(fixture.representative.account.address),
    );
  });

  it("rejects permit replay, pointer substitution, and the wrong submitter", async function () {
    const { registry, claimant, representative, permit, permitSignature } =
      await signedClaimPermit();

    await viem.assertions.revertWithCustomError(
      registry.write.submitClaimWithPermit(
        [permit, dataPointer, permitSignature],
        { account: representative.account },
      ),
      registry,
      "ClaimPermitSubmitterMismatch",
    );
    await viem.assertions.revertWithCustomError(
      registry.write.submitClaimWithPermit(
        [permit, "ipfs://bafydifferentcid", permitSignature],
        { account: claimant.account },
      ),
      registry,
      "ClaimPermitPointerMismatch",
    );
    await registry.write.submitClaimWithPermit(
      [permit, dataPointer, permitSignature],
      { account: claimant.account },
    );
    await viem.assertions.revertWithCustomError(
      registry.write.submitClaimWithPermit(
        [permit, dataPointer, permitSignature],
        { account: claimant.account },
      ),
      registry,
      "ClaimPermitAlreadyUsed",
    );
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

  it("allows one scoring assessor to be scoped to multiple insurers", async function () {
    const { registry, admin, insurer, assessor, otherInsurer } =
      await deployFixture();
    await registry.write.setSubmitter([otherInsurer.account.address, true], {
      account: admin.account,
    });
    await registry.write.setAssessor(
      [assessor.account.address, otherInsurer.account.address, true],
      { account: admin.account },
    );

    assert.equal(
      await registry.read.isAssessorFor([
        assessor.account.address,
        insurer.account.address,
      ]),
      true,
    );
    assert.equal(
      await registry.read.isAssessorFor([
        assessor.account.address,
        otherInsurer.account.address,
      ]),
      true,
    );
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: otherInsurer.account,
    });
    await registry.write.assessClaim([0n, Status.UnderReview, 1200], {
      account: assessor.account,
    });
  });

  it("separates model screening from the terminal coverage decision", async function () {
    const { registry, insurer, assessor, decisionMaker } =
      await deployFixture();
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: insurer.account,
    });

    await registry.write.assessClaim([0n, Status.UnderReview, 4200], {
      account: assessor.account,
    });
    await viem.assertions.revertWithCustomError(
      registry.write.assessClaim([0n, Status.Approved, 4200], {
        account: assessor.account,
      }),
      registry,
      "InvalidStatusTransition",
    );

    const decisionHash = keccak256(toHex("decision:claim-0:approved:v1"));
    await registry.write.decideClaim(
      [0n, Status.Approved, decisionHash],
      { account: decisionMaker.account },
    );
    const storedDecision = await registry.read.getClaimDecision([0n]);
    assert.equal(storedDecision[0], decisionHash);
    assert.equal(
      storedDecision[1],
      getAddress(decisionMaker.account.address),
    );

    await viem.assertions.revertWithCustomError(
      registry.write.decideClaim(
        [0n, Status.Rejected, decisionHash],
        { account: decisionMaker.account },
      ),
      registry,
      "InvalidStatusTransition",
    );
  });

  it("does not allow the one-time model screening to be rewritten", async function () {
    const { registry, insurer, assessor } = await deployFixture();
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: insurer.account,
    });
    await registry.write.assessClaim([0n, Status.Flagged, 8500], {
      account: assessor.account,
    });

    await viem.assertions.revertWithCustomError(
      registry.write.assessClaim([0n, Status.UnderReview, 100], {
        account: assessor.account,
      }),
      registry,
      "InvalidStatusTransition",
    );
  });

  it("scopes decision makers to their insurer", async function () {
    const {
      registry,
      admin,
      insurer,
      assessor,
      otherInsurer,
      otherDecisionMaker,
    } = await deployFixture();
    await registry.write.setSubmitter([otherInsurer.account.address, true], {
      account: admin.account,
    });
    await registry.write.setDecisionMaker(
      [
        otherDecisionMaker.account.address,
        otherInsurer.account.address,
        true,
      ],
      { account: admin.account },
    );
    await registry.write.submitClaim([claimHash, dataPointer], {
      account: insurer.account,
    });
    await registry.write.assessClaim([0n, Status.UnderReview, 4200], {
      account: assessor.account,
    });

    await viem.assertions.revertWithCustomError(
      registry.write.decideClaim(
        [0n, Status.Rejected, keccak256(toHex("decision:rejected"))],
        { account: otherDecisionMaker.account },
      ),
      registry,
      "DecisionMakerScopeMismatch",
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
