// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {ClaimsForwarder} from "./ClaimsForwarder.sol";
import {ClaimsRegistry} from "./ClaimsRegistry.sol";
import {Test} from "forge-std/Test.sol";

contract ClaimsRegistryTest is Test {
  ClaimsRegistry registry;
  ClaimsForwarder forwarder;

  address admin = makeAddr("admin");
  address insurer = makeAddr("insurer");
  address assessor = makeAddr("assessor");
  address otherInsurer = makeAddr("otherInsurer");
  address otherAssessor = makeAddr("otherAssessor");

  bytes32 constant CLAIM_HASH = keccak256("policy-42:incident-2026-07-13");
  string constant DATA_POINTER = "ipfs://bafydemocid";

  event ClaimSubmitted(
    uint256 indexed claimId,
    address indexed claimant,
    bytes32 claimHash,
    string dataPointer,
    uint256 timestamp
  );
  event ClaimAssessed(
    uint256 indexed claimId,
    ClaimsRegistry.Status indexed newStatus,
    address indexed assessor,
    uint16 fraudScore,
    uint256 timestamp
  );

  function setUp() public {
    forwarder = new ClaimsForwarder();
    registry = new ClaimsRegistry(admin, insurer, assessor, address(forwarder), 0);
  }

  function test_TrustedForwarderRestoresInsurerIdentity() public {
    bytes memory callData = abi.encodeCall(
      registry.submitClaim,
      (CLAIM_HASH, DATA_POINTER)
    );

    vm.prank(address(forwarder));
    (bool success, ) = address(registry).call(
      abi.encodePacked(callData, insurer)
    );

    assertTrue(success);
    (address storedClaimant, , , , , , ) = registry.getClaim(0);
    assertEq(storedClaimant, insurer);
  }

  function test_UntrustedCallerCannotForgeInsurerIdentity() public {
    bytes memory callData = abi.encodeCall(
      registry.submitClaim,
      (CLAIM_HASH, DATA_POINTER)
    );

    vm.prank(otherInsurer);
    (bool success, ) = address(registry).call(
      abi.encodePacked(callData, insurer)
    );

    assertFalse(success);
    assertEq(registry.claimCount(), 0);
  }

  function test_SubmitClaimStoresAndEmits() public {
    vm.warp(1_000_000);
    vm.expectEmit(true, true, false, true);
    emit ClaimSubmitted(0, insurer, CLAIM_HASH, DATA_POINTER, 1_000_000);

    vm.prank(insurer);
    uint256 id = registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    assertEq(id, 0);
    assertEq(registry.claimCount(), 1);
    (
      address storedClaimant,
      bytes32 storedHash,
      string memory storedPointer,
      ClaimsRegistry.Status status,
      uint16 fraudScore,
      uint64 submittedAt,
      uint64 updatedAt
    ) = registry.getClaim(0);
    assertEq(storedClaimant, insurer);
    assertEq(storedHash, CLAIM_HASH);
    assertEq(storedPointer, DATA_POINTER);
    assertEq(uint8(status), uint8(ClaimsRegistry.Status.Submitted));
    assertEq(fraudScore, 0);
    assertEq(submittedAt, 1_000_000);
    assertEq(updatedAt, 1_000_000);
  }

  function test_RevertWhen_SubmitterIsUnauthorized() public {
    vm.expectRevert(
      abi.encodeWithSelector(
        IAccessControl.AccessControlUnauthorizedAccount.selector,
        otherInsurer,
        registry.SUBMITTER_ROLE()
      )
    );
    vm.prank(otherInsurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);
  }

  function test_RevertWhen_EmptyHash() public {
    vm.prank(insurer);
    vm.expectRevert(ClaimsRegistry.EmptyClaimHash.selector);
    registry.submitClaim(bytes32(0), DATA_POINTER);
  }

  function test_RevertWhen_PointerIsNotBareIpfsCid() public {
    vm.startPrank(insurer);
    vm.expectRevert(ClaimsRegistry.InvalidDataPointer.selector);
    registry.submitClaim(CLAIM_HASH, "https://example.test/claim");
    vm.expectRevert(ClaimsRegistry.InvalidDataPointer.selector);
    registry.submitClaim(CLAIM_HASH, "ipfs://bad/path");
    vm.stopPrank();
  }

  function test_RevertWhen_PointerIsTooLong() public {
    bytes memory oversized = new bytes(129);
    for (uint256 index; index < oversized.length; ++index) {
      oversized[index] = "a";
    }
    vm.prank(insurer);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.DataPointerTooLong.selector,
        oversized.length
      )
    );
    registry.submitClaim(CLAIM_HASH, string(oversized));
  }

  function test_AssessUpdatesClaimAndEmits() public {
    vm.prank(insurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.warp(2_000_000);
    vm.expectEmit(true, true, true, true);
    emit ClaimAssessed(
      0,
      ClaimsRegistry.Status.Flagged,
      assessor,
      8500,
      2_000_000
    );
    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, 8500);

    (, , , ClaimsRegistry.Status status, uint16 fraudScore, , uint64 updatedAt) =
      registry.getClaim(0);
    assertEq(uint8(status), uint8(ClaimsRegistry.Status.Flagged));
    assertEq(fraudScore, 8500);
    assertEq(updatedAt, 2_000_000);
  }

  function test_RevertWhen_AssessorBelongsToAnotherInsurer() public {
    vm.startPrank(admin);
    registry.setSubmitter(otherInsurer, true);
    registry.setAssessor(otherAssessor, otherInsurer, true);
    vm.stopPrank();
    vm.prank(insurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.prank(otherAssessor);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.AssessorScopeMismatch.selector,
        otherAssessor,
        insurer
      )
    );
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, 5000);
  }

  function test_AssessorCanBeScopedToMultipleInsurers() public {
    vm.startPrank(admin);
    registry.setSubmitter(otherInsurer, true);
    registry.setAssessor(assessor, otherInsurer, true);
    vm.stopPrank();

    assertTrue(registry.isAssessorFor(assessor, insurer));
    assertTrue(registry.isAssessorFor(assessor, otherInsurer));

    vm.prank(otherInsurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);
    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.UnderReview, 1200);
  }

  function test_RevokeOneScopeKeepsOtherAssessorScopeActive() public {
    vm.startPrank(admin);
    registry.setSubmitter(otherInsurer, true);
    registry.setAssessor(assessor, otherInsurer, true);
    registry.setAssessor(assessor, insurer, false);
    vm.stopPrank();

    assertTrue(registry.isAssessor(assessor));
    assertFalse(registry.isAssessorFor(assessor, insurer));
    assertTrue(registry.isAssessorFor(assessor, otherInsurer));
  }

  function test_LifecycleIsMonotonicAndScoreIsImmutable() public {
    vm.prank(insurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);
    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.UnderReview, 4200);

    vm.prank(assessor);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.InvalidStatusTransition.selector,
        ClaimsRegistry.Status.UnderReview,
        ClaimsRegistry.Status.Submitted
      )
    );
    registry.assessClaim(0, ClaimsRegistry.Status.Submitted, 4200);

    vm.prank(assessor);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.FraudScoreCannotChange.selector,
        4200,
        5000
      )
    );
    registry.assessClaim(0, ClaimsRegistry.Status.Approved, 5000);

    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.Approved, 4200);
    vm.prank(assessor);
    vm.expectRevert();
    registry.assessClaim(0, ClaimsRegistry.Status.Rejected, 4200);
  }

  function test_RevertWhen_UnknownClaim() public {
    vm.prank(assessor);
    vm.expectRevert(
      abi.encodeWithSelector(ClaimsRegistry.UnknownClaim.selector, 99)
    );
    registry.assessClaim(99, ClaimsRegistry.Status.Flagged, 0);
  }

  function test_RevertWhen_ScoreExceedsBasisPointLimit() public {
    vm.prank(insurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);
    vm.prank(assessor);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.InvalidFraudScore.selector,
        10001
      )
    );
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, 10001);
  }

  function test_AdminTransferRequiresNewAdminAcceptance() public {
    vm.prank(admin);
    registry.beginDefaultAdminTransfer(otherInsurer);
    assertEq(registry.owner(), admin);

    vm.warp(block.timestamp + 1);
    vm.prank(otherInsurer);
    registry.acceptDefaultAdminTransfer();
    assertEq(registry.owner(), otherInsurer);
  }

  function test_PrivilegedRolesMustUseDifferentAccounts() public {
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.RoleSeparationRequired.selector,
        admin
      )
    );
    vm.prank(admin);
    registry.setSubmitter(admin, true);

    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.RoleSeparationRequired.selector,
        insurer
      )
    );
    vm.prank(admin);
    registry.setAssessor(insurer, insurer, true);

    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.RoleSeparationRequired.selector,
        insurer
      )
    );
    vm.prank(admin);
    registry.beginDefaultAdminTransfer(insurer);

    vm.prank(admin);
    registry.beginDefaultAdminTransfer(otherInsurer);
    vm.prank(admin);
    registry.setSubmitter(otherInsurer, true);
    vm.warp(block.timestamp + 1);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.RoleSeparationRequired.selector,
        otherInsurer
      )
    );
    vm.prank(otherInsurer);
    registry.acceptDefaultAdminTransfer();
  }

  function test_RoleInvariantsCannotBeBypassedThroughGrantRole() public {
    bytes32 assessorRole = registry.ASSESSOR_ROLE();
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.UseRoleConfigurationFunction.selector,
        assessorRole
      )
    );
    vm.prank(admin);
    registry.grantRole(assessorRole, otherAssessor);
  }

  function test_VerifyClaimData() public {
    bytes memory payload = "the canonical off-chain claim payload";
    vm.prank(insurer);
    registry.submitClaim(keccak256(payload), DATA_POINTER);
    assertTrue(registry.verifyClaimData(0, payload));
    assertFalse(registry.verifyClaimData(0, "tampered payload"));
  }

  function testFuzz_RejectsScoresAbove10000(uint16 score) public {
    vm.assume(score > 10000);
    vm.prank(insurer);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);
    vm.prank(assessor);
    vm.expectRevert(
      abi.encodeWithSelector(
        ClaimsRegistry.InvalidFraudScore.selector,
        score
      )
    );
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, score);
  }

  function testFuzz_SubmitAnyNonZeroHash(bytes32 hash) public {
    vm.assume(hash != bytes32(0));
    vm.prank(insurer);
    uint256 id = registry.submitClaim(hash, DATA_POINTER);
    (, bytes32 storedHash, , , , , ) = registry.getClaim(id);
    assertEq(storedHash, hash);
  }
}
