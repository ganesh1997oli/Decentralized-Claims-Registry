// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ClaimsRegistry} from "./ClaimsRegistry.sol";
import {Test} from "forge-std/Test.sol";

contract ClaimsRegistryTest is Test {
  ClaimsRegistry registry;

  address claimant = makeAddr("claimant");
  address assessor = makeAddr("assessor");

  bytes32 constant CLAIM_HASH = keccak256("policy-42:incident-2026-07-13");
  string constant DATA_POINTER = "ipfs://bafy-demo-cid";

  // Redeclared locally so vm.expectEmit can compare against them.
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
    registry = new ClaimsRegistry(); // this test contract becomes owner
    registry.setAssessor(assessor, true);
  }

  function test_SubmitClaimStoresAndEmits() public {
    vm.warp(1_000_000);

    vm.expectEmit(true, true, false, true);
    emit ClaimSubmitted(0, claimant, CLAIM_HASH, DATA_POINTER, 1_000_000);

    vm.prank(claimant);
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

    assertEq(storedClaimant, claimant);
    assertEq(storedHash, CLAIM_HASH);
    assertEq(storedPointer, DATA_POINTER);
    assertEq(uint8(status), uint8(ClaimsRegistry.Status.Submitted));
    assertEq(fraudScore, 0);
    assertEq(submittedAt, 1_000_000);
    assertEq(updatedAt, 1_000_000);
  }

  function test_RevertWhen_EmptyHash() public {
    vm.prank(claimant);
    vm.expectRevert(ClaimsRegistry.EmptyClaimHash.selector);
    registry.submitClaim(bytes32(0), DATA_POINTER);
  }

  function test_AssessUpdatesClaimAndEmits() public {
    vm.prank(claimant);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.warp(2_000_000);
    vm.expectEmit(true, true, true, true);
    emit ClaimAssessed(0, ClaimsRegistry.Status.Flagged, assessor, 8500, 2_000_000);

    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, 8500);

    (, , , ClaimsRegistry.Status status, uint16 fraudScore, , uint64 updatedAt) =
      registry.getClaim(0);
    assertEq(uint8(status), uint8(ClaimsRegistry.Status.Flagged));
    assertEq(fraudScore, 8500);
    assertEq(updatedAt, 2_000_000);
  }

  function test_RevertWhen_NotAssessor() public {
    vm.prank(claimant);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.prank(claimant);
    vm.expectRevert(ClaimsRegistry.NotAssessor.selector);
    registry.assessClaim(0, ClaimsRegistry.Status.Approved, 0);
  }

  function test_RevertWhen_UnknownClaim() public {
    vm.prank(assessor);
    vm.expectRevert(abi.encodeWithSelector(ClaimsRegistry.UnknownClaim.selector, 99));
    registry.assessClaim(99, ClaimsRegistry.Status.Approved, 0);
  }

  function test_RevertWhen_AlreadyFinalized() public {
    vm.prank(claimant);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.prank(assessor);
    registry.assessClaim(0, ClaimsRegistry.Status.Approved, 100);

    vm.prank(assessor);
    vm.expectRevert(abi.encodeWithSelector(ClaimsRegistry.ClaimAlreadyFinalized.selector, 0));
    registry.assessClaim(0, ClaimsRegistry.Status.Rejected, 200);
  }

  function test_RevertWhen_NonOwnerSetsAssessor() public {
    vm.prank(claimant);
    vm.expectRevert(ClaimsRegistry.NotOwner.selector);
    registry.setAssessor(claimant, true);
  }

  function test_VerifyClaimData() public {
    bytes memory payload = "the canonical off-chain claim payload";
    vm.prank(claimant);
    registry.submitClaim(keccak256(payload), DATA_POINTER);

    assertTrue(registry.verifyClaimData(0, payload));
    assertFalse(registry.verifyClaimData(0, "tampered payload"));
  }

  // Fuzz: every score above the 10000 bps cap must revert, whatever it is.
  function testFuzz_RejectsScoresAbove10000(uint16 score) public {
    vm.assume(score > 10000);
    vm.prank(claimant);
    registry.submitClaim(CLAIM_HASH, DATA_POINTER);

    vm.prank(assessor);
    vm.expectRevert(abi.encodeWithSelector(ClaimsRegistry.InvalidFraudScore.selector, score));
    registry.assessClaim(0, ClaimsRegistry.Status.Flagged, score);
  }

  // Fuzz: any non-zero hash round-trips through storage unchanged.
  function testFuzz_SubmitAnyNonZeroHash(bytes32 h) public {
    vm.assume(h != bytes32(0));
    vm.prank(claimant);
    uint256 id = registry.submitClaim(h, DATA_POINTER);

    (, bytes32 stored, , , , , ) = registry.getClaim(id);
    assertEq(stored, h);
  }
}