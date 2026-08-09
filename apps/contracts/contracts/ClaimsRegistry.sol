// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {
    AccessControlDefaultAdminRules
} from "@openzeppelin/contracts/access/extensions/AccessControlDefaultAdminRules.sol";
import {ERC2771Context} from "@openzeppelin/contracts/metatx/ERC2771Context.sol";
import {Context} from "@openzeppelin/contracts/utils/Context.sol";

/// @title ClaimsRegistry
/// @notice Anchors synthetic insurance claims while keeping their payloads
///         off-chain. The pointer and hash are public; they must never reference
///         personal, confidential, or unencrypted real-claim data.
contract ClaimsRegistry is AccessControlDefaultAdminRules, ERC2771Context {
    bytes32 public constant SUBMITTER_ROLE = keccak256("SUBMITTER_ROLE");
    bytes32 public constant ASSESSOR_ROLE = keccak256("ASSESSOR_ROLE");
    uint256 public constant MAX_DATA_POINTER_LENGTH = 128;

    enum Status {
        Submitted,
        UnderReview,
        Approved,
        Rejected,
        Flagged
    }

    struct Claim {
        address claimant;
        bytes32 claimHash;
        string dataPointer;
        Status status;
        uint16 fraudScore;
        uint64 submittedAt;
        uint64 updatedAt;
        bool exists;
    }

    uint256 public claimCount;
    mapping(uint256 claimId => Claim claim) private _claims;
    mapping(address assessor => mapping(address insurer => bool authorized))
        private _assessorScopes;
    mapping(address assessor => uint256 scopeCount) private _assessorScopeCount;

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
    event SubmitterUpdated(address indexed submitter, bool authorized);
    event AssessorUpdated(
        address indexed assessor,
        address indexed insurer,
        bool authorized
    );

    error ZeroAddress();
    error EmptyClaimHash();
    error InvalidDataPointer();
    error DataPointerTooLong(uint256 suppliedLength);
    error UnknownClaim(uint256 claimId);
    error InvalidFraudScore(uint16 fraudScore);
    error InvalidStatusTransition(Status currentStatus, Status requestedStatus);
    error FraudScoreCannotChange(uint16 currentScore, uint16 suppliedScore);
    error AssessorScopeMismatch(address assessor, address claimSubmitter);
    error AssessorScopeNotConfigured(address assessor, address insurer);
    error InsurerNotAuthorized(address insurer);
    error RoleSeparationRequired(address account);
    error UseRoleConfigurationFunction(bytes32 role);

    /// @param initialAdmin Account that controls role assignment and starts
    ///        delayed, two-step admin transfers.
    /// @param initialSubmitter Insurer service account permitted to submit.
    /// @param initialAssessor Scoring account scoped to initialSubmitter.
    /// @param trustedForwarder Immutable ERC-2771 forwarder that verifies
    ///        insurer signatures before restoring their execution context.
    /// @param adminTransferDelay Delay in seconds before an admin transfer can
    ///        be accepted. Production deployments should use a non-zero delay.
    constructor(
        address initialAdmin,
        address initialSubmitter,
        address initialAssessor,
        address trustedForwarder,
        uint48 adminTransferDelay
    )
        AccessControlDefaultAdminRules(adminTransferDelay, initialAdmin)
        ERC2771Context(trustedForwarder)
    {
        if (
            initialSubmitter == address(0) ||
            initialAssessor == address(0) ||
            trustedForwarder == address(0)
        ) {
            revert ZeroAddress();
        }
        if (
            initialAdmin == initialSubmitter ||
            initialAdmin == initialAssessor
        ) {
            revert RoleSeparationRequired(initialAdmin);
        }
        if (initialSubmitter == initialAssessor) {
            revert RoleSeparationRequired(initialSubmitter);
        }

        _grantRole(SUBMITTER_ROLE, initialSubmitter);
        _grantRole(ASSESSOR_ROLE, initialAssessor);
        _assessorScopes[initialAssessor][initialSubmitter] = true;
        _assessorScopeCount[initialAssessor] = 1;

        emit SubmitterUpdated(initialSubmitter, true);
        emit AssessorUpdated(initialAssessor, initialSubmitter, true);
    }

    /// @notice Record a synthetic claim and its public IPFS CID.
    function submitClaim(
        bytes32 claimHash,
        string calldata dataPointer
    ) external onlyRole(SUBMITTER_ROLE) returns (uint256 claimId) {
        if (claimHash == bytes32(0)) revert EmptyClaimHash();
        _validateDataPointer(dataPointer);
        address submitter = _msgSender();

        claimId = claimCount;
        _claims[claimId] = Claim({
            claimant: submitter,
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
            submitter,
            claimHash,
            dataPointer,
            block.timestamp
        );
    }

    /// @notice Advance a claim through its allowed lifecycle.
    /// @dev The first assessment fixes the fraud score. Later status changes
    ///      must carry the same score, preserving the original model output.
    function assessClaim(
        uint256 claimId,
        Status newStatus,
        uint16 fraudScore
    ) external onlyRole(ASSESSOR_ROLE) {
        Claim storage claim = _claims[claimId];
        address assessor = _msgSender();
        if (!claim.exists) revert UnknownClaim(claimId);
        if (!_assessorScopes[assessor][claim.claimant]) {
            revert AssessorScopeMismatch(assessor, claim.claimant);
        }
        if (fraudScore > 10000) revert InvalidFraudScore(fraudScore);
        if (!_isAllowedTransition(claim.status, newStatus)) {
            revert InvalidStatusTransition(claim.status, newStatus);
        }
        if (
            claim.status != Status.Submitted &&
            fraudScore != claim.fraudScore
        ) {
            revert FraudScoreCannotChange(claim.fraudScore, fraudScore);
        }

        claim.status = newStatus;
        claim.fraudScore = fraudScore;
        claim.updatedAt = uint64(block.timestamp);

        emit ClaimAssessed(
            claimId,
            newStatus,
            assessor,
            fraudScore,
            block.timestamp
        );
    }

    /// @notice Return the current compact public record for one claim.
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

    /// @notice Compare supplied off-chain bytes with the permanent claim hash.
    function verifyClaimData(
        uint256 claimId,
        bytes calldata payload
    ) external view returns (bool) {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        return keccak256(payload) == claim.claimHash;
    }

    /// @notice Report whether an account may create new claim anchors.
    function isSubmitter(address account) external view returns (bool) {
        return hasRole(SUBMITTER_ROLE, account);
    }

    /// @notice Report whether an account may assess claims in its insurer scope.
    function isAssessor(address account) external view returns (bool) {
        return hasRole(ASSESSOR_ROLE, account);
    }

    /// @notice Report whether an assessor may update one insurer's claims.
    function isAssessorFor(
        address assessor,
        address insurer
    ) external view returns (bool) {
        return
            hasRole(ASSESSOR_ROLE, assessor) &&
            _assessorScopes[assessor][insurer];
    }

    /// @notice Grant or revoke claim-submission permission.
    function setSubmitter(
        address submitter,
        bool authorized
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (submitter == address(0)) revert ZeroAddress();
        if (authorized) {
            if (
                submitter == defaultAdmin() ||
                hasRole(ASSESSOR_ROLE, submitter)
            ) {
                revert RoleSeparationRequired(submitter);
            }
            _grantRole(SUBMITTER_ROLE, submitter);
        } else {
            _revokeRole(SUBMITTER_ROLE, submitter);
        }
        emit SubmitterUpdated(submitter, authorized);
    }

    /// @notice Grant a scoped assessor or revoke its existing scope.
    function setAssessor(
        address assessor,
        address insurer,
        bool authorized
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (assessor == address(0) || insurer == address(0)) {
            revert ZeroAddress();
        }
        if (authorized) {
            if (!hasRole(SUBMITTER_ROLE, insurer)) {
                revert InsurerNotAuthorized(insurer);
            }
            if (
                assessor == defaultAdmin() ||
                hasRole(SUBMITTER_ROLE, assessor)
            ) {
                revert RoleSeparationRequired(assessor);
            }
            if (!_assessorScopes[assessor][insurer]) {
                _assessorScopes[assessor][insurer] = true;
                unchecked {
                    ++_assessorScopeCount[assessor];
                }
                _grantRole(ASSESSOR_ROLE, assessor);
            }
        } else {
            if (!_assessorScopes[assessor][insurer]) {
                revert AssessorScopeNotConfigured(assessor, insurer);
            }
            delete _assessorScopes[assessor][insurer];
            uint256 remainingScopes = _assessorScopeCount[assessor] - 1;
            _assessorScopeCount[assessor] = remainingScopes;
            if (remainingScopes == 0) {
                _revokeRole(ASSESSOR_ROLE, assessor);
            }
        }
        emit AssessorUpdated(assessor, insurer, authorized);
    }

    /// @notice Start OpenZeppelin's delayed two-step admin transfer.
    function beginDefaultAdminTransfer(
        address newAdmin
    ) public override onlyRole(DEFAULT_ADMIN_ROLE) {
        if (
            newAdmin != address(0) &&
            (
                hasRole(SUBMITTER_ROLE, newAdmin) ||
                hasRole(ASSESSOR_ROLE, newAdmin)
            )
        ) {
            revert RoleSeparationRequired(newAdmin);
        }
        super.beginDefaultAdminTransfer(newAdmin);
    }

    /// @notice Accept admin control after the configured delay has elapsed.
    function acceptDefaultAdminTransfer() public override {
        address sender = _msgSender();
        if (
            hasRole(SUBMITTER_ROLE, sender) ||
            hasRole(ASSESSOR_ROLE, sender)
        ) {
            revert RoleSeparationRequired(sender);
        }
        super.acceptDefaultAdminTransfer();
    }

    /// @dev Resolve the multiple Context inheritance in favour of ERC-2771.
    ///      Direct calls still return the EVM sender, while forwarded calls
    ///      return the EIP-712 signer appended by the immutable forwarder.
    function _msgSender()
        internal
        view
        override(Context, ERC2771Context)
        returns (address)
    {
        return ERC2771Context._msgSender();
    }

    /// @dev Strip the ERC-2771 signer suffix for forwarded calls.
    function _msgData()
        internal
        view
        override(Context, ERC2771Context)
        returns (bytes calldata)
    {
        return ERC2771Context._msgData();
    }

    /// @dev ERC-2771 appends exactly one 20-byte signer address.
    function _contextSuffixLength()
        internal
        view
        override(Context, ERC2771Context)
        returns (uint256)
    {
        return ERC2771Context._contextSuffixLength();
    }

    /// @dev Scoped roles must be configured through setSubmitter/setAssessor so
    ///      their accompanying security invariants cannot be bypassed.
    function grantRole(
        bytes32 role,
        address account
    ) public override {
        if (role == SUBMITTER_ROLE || role == ASSESSOR_ROLE) {
            revert UseRoleConfigurationFunction(role);
        }
        super.grantRole(role, account);
    }

    /// @dev See grantRole: scoped roles must use the invariant-preserving APIs.
    function revokeRole(
        bytes32 role,
        address account
    ) public override {
        if (role == SUBMITTER_ROLE || role == ASSESSOR_ROLE) {
            revert UseRoleConfigurationFunction(role);
        }
        super.revokeRole(role, account);
    }

    /// @dev Final states deliberately have no outgoing transitions.
    function _isAllowedTransition(
        Status currentStatus,
        Status requestedStatus
    ) private pure returns (bool) {
        if (currentStatus == Status.Submitted) {
            return
                requestedStatus == Status.UnderReview ||
                requestedStatus == Status.Flagged;
        }
        if (currentStatus == Status.UnderReview) {
            return
                requestedStatus == Status.Approved ||
                requestedStatus == Status.Rejected ||
                requestedStatus == Status.Flagged;
        }
        if (currentStatus == Status.Flagged) {
            return
                requestedStatus == Status.Approved ||
                requestedStatus == Status.Rejected;
        }
        return false;
    }

    /// @dev This prototype stores a bare IPFS CID only. Requiring an
    ///      alphanumeric target prevents malformed paths, query strings and
    ///      unsupported URL schemes from becoming permanent poison events.
    function _validateDataPointer(string calldata dataPointer) private pure {
        bytes calldata pointer = bytes(dataPointer);
        if (pointer.length > MAX_DATA_POINTER_LENGTH) {
            revert DataPointerTooLong(pointer.length);
        }
        if (
            pointer.length <= 7 ||
            pointer[0] != "i" ||
            pointer[1] != "p" ||
            pointer[2] != "f" ||
            pointer[3] != "s" ||
            pointer[4] != ":" ||
            pointer[5] != "/" ||
            pointer[6] != "/"
        ) {
            revert InvalidDataPointer();
        }

        for (uint256 index = 7; index < pointer.length; ++index) {
            bytes1 character = pointer[index];
            bool isNumber = character >= "0" && character <= "9";
            bool isUppercase = character >= "A" && character <= "Z";
            bool isLowercase = character >= "a" && character <= "z";
            if (!isNumber && !isUppercase && !isLowercase) {
                revert InvalidDataPointer();
            }
        }
    }
}
