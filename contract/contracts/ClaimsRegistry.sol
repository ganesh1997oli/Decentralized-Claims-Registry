// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.28;

/// ClaimRegistry
/// On-Chain registry that anchors insurance claims to an immutable,
/// verifiable ledger while keeping the sensitive claim data OFF-chain.
/// Only a cryptographic hash of the off-chain payload plus a small set
/// of non-sensitive metadata are stored here. Authorized assessors
/// (e.g. an insurer backend or a fraud-detection oracle) write
/// assessment results back to a claim; downstream systems observe the whole
/// lifecycle through the emitted events.
///
/// Access control here is intentionally minimal and self-contained so
/// the contract here is intentionally minimal and selff-contained so
/// prefer OpenZeppelin's audited `Ownable` / `AccessControl` over the
/// hand-rolled owner+assessor logic below.

contract ClaimsRegistry {
    //
    // Types
    //

    enum Status {
        Submitted, // 0 - recorded, awaiting review
        UnderReview, // 1 - being assessed
        Approved, // 2 - final: accepted
        Rejected, // 3 - final: denied
        Flagged // 4 - suspected fraud
    }

    struct Claim {
        address claimant; // who submitted the claim
        bytes32 claimHash; // keccak256 of the canonical off-chain payload
        string dataPointer; // off-chain location (e.g. IPFS CID or URL)
        Status status; // current lifecycle state
        uint16 fraudScore; // model output in basis points (0.10000); 0 until assessed
        uint64 submittedAt; // block timestamp at submission
        uint64 updatedAt; // block timestamp of last status change
        bool exists; // distinguishes a real claim from an unset slot
    }

    //
    // Storage
    //

    address public owner;
    uint256 public claimCount; // also the id of the next claim
    mapping(uint256 => Claim) private _claims;
    mapping(address => bool) public isAssessor;

    //
    // Events (indexed fields chosen so off-chain listeners can filter cheaply)
    //

    event ClaimSubmitted(
        uint256 indexed claimId,
        address indexed claimant,
        bytes32 claimHash,
        string dataPointer,
        uint256 timestamp
    );

    event ClaimAssessed(
        uint256 indexed claimId,
        Status indexed newStatus,
        address indexed assessor,
        uint16 fraudScore,
        uint256 timestamp
    );

    event AssessorUpdated(address indexed assessor, bool authorized);

    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );

    //
    // Errors
    //

    error NotOwner();
    error NotAssessor();
    error ZeroAddress();
    error EmptyClaimHash();
    error UnknownClaim(uint256 claimId);
    error ClaimAlreadyFinalized(uint256 claimId);
    error InvalidFraudScore(uint16 fraudScore);

    //
    // Modifiers
    //

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyAssessor() {
        if (!isAssessor[msg.sender]) revert NotAssessor();
        _;
    }

    //
    // Constructor
    //

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    //
    // Claim lifecycle
    //

    /// Record a new claim on-chain
    /// claimHash keccak256 hash of the full off-chain claim payload.
    /// dataPointer off-chain location of the payload (e.g. an IPFS CID).
    /// claimId The id assigned to this claim.
    function submitClaim(
        bytes32 claimHash,
        string calldata dataPointer
    ) external returns (uint256 claimId) {
        if (claimHash == bytes32(0)) revert EmptyClaimHash();

        claimId = claimCount;
        _claims[claimId] = Claim({
            claimant: msg.sender,
            claimHash: claimHash,
            dataPointer: dataPointer,
            status: Status.Submitted,
            fraudScore: 0,
            submittedAt: uint64(block.timestamp),
            updatedAt: uint64(block.timestamp),
            exists: true
        });

        unchecked {
            claimCount = claimId + 1;
        }

        emit ClaimSubmitted(
            claimId,
            msg.sender,
            claimHash,
            dataPointer,
            block.timestamp
        );
    }

    /// Write an assessment result back to a claim. Intended for the
    /// fraud-detection oracle / insurer backend.
    /// claimId The claim to update.
    /// newStatus New lifecycle status.
    /// fraudScore Model score in basis points (0-10000).
    function assessClaim(
        uint256 claimId,
        Status newStatus,
        uint16 fraudScore
    ) external onlyAssessor {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        if (
            claim.status == Status.Approved || claim.status == Status.Rejected
        ) {
            revert ClaimAlreadyFinalized(claimId);
        }
        if (fraudScore > 10000) revert InvalidFraudScore(fraudScore);

        claim.status = newStatus;
        claim.fraudScore = fraudScore;
        claim.updatedAt = uint64(block.timestamp);

        emit ClaimAssessed(
            claimId,
            newStatus,
            msg.sender,
            fraudScore,
            block.timestamp
        );
    }

    //
    // Views
    //

    /// Read a claim by id. Reverts if the claim does not exists.
    function getClaim(
        uint256 claimId
    )
        external
        view
        returns (
            address claimant,
            bytes32 claimHash,
            string memory dataPointer,
            Status status,
            uint16 fraudScore,
            uint64 submittedAt,
            uint64 updatedAt
        )
    {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        return (
            claim.claimant,
            claim.claimHash,
            claim.dataPointer,
            claim.status,
            claim.fraudScore,
            claim.submittedAt,
            claim.updatedAt
        );
    }

    /// Confirm that an off-chain payload matches the stored hash.
    /// The payload must be hashed the same way it was before submission
    /// (keccak256 over the exact canonical bytes).
    function verifyClaimData(
        uint256 claimId,
        bytes calldata payload
    ) external view returns (bool) {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        return keccak256(payload) == claim.claimHash;
    }

    //
    // Access control admin
    //

    /// Grant or revoke assessor rights rights (who may call `assessClaim).
    function setAssessor(address assessor, bool authorized) external onlyOwner {
        if (assessor == address(0)) revert ZeroAddress();
        isAssessor[assessor] = authorized;
        emit AssessorUpdated(assessor, authorized);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
}
