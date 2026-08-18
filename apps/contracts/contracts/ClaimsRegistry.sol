// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {
    AccessControlDefaultAdminRules
} from "@openzeppelin/contracts/access/extensions/AccessControlDefaultAdminRules.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ERC2771Context} from "@openzeppelin/contracts/metatx/ERC2771Context.sol";
import {Context} from "@openzeppelin/contracts/utils/Context.sol";

/// @title ClaimsRegistry
/// @notice Anchors synthetic insurance claims while keeping their payloads
///         off-chain. The pointer and hash are public; they must never reference
///         personal, confidential, or unencrypted real-claim data.
/// @dev Insurer-operated submissions remain available for controlled migration.
///      Public submissions use two independent proofs: the submitter signs the
///      ERC-2771 request and an insurer-scoped permit issuer signs the exact
///      eligible claim. The relayer supplies only gas and receives no registry
///      role or authority over claim contents.
contract ClaimsRegistry is
    AccessControlDefaultAdminRules,
    ERC2771Context,
    EIP712
{
    bytes32 public constant SUBMITTER_ROLE = keccak256("SUBMITTER_ROLE");
    bytes32 public constant ASSESSOR_ROLE = keccak256("ASSESSOR_ROLE");
    bytes32 public constant DECISION_MAKER_ROLE =
        keccak256("DECISION_MAKER_ROLE");
    bytes32 public constant PERMIT_ISSUER_ROLE =
        keccak256("PERMIT_ISSUER_ROLE");
    bytes32 public constant CLAIM_PERMIT_TYPEHASH =
        keccak256(
            "ClaimPermit(address claimant,address submitter,address insurer,bytes32 claimantCommitment,bytes32 claimHash,bytes32 dataPointerHash,bytes32 permitId,uint48 deadline)"
        );
    uint256 public constant MAX_DATA_POINTER_LENGTH = 128;

    /// @dev Stored lifecycle values. Approved and Rejected are terminal; Flagged
    ///      can still be resolved to either of those terminal decisions.
    enum Status {
        Submitted,
        UnderReview,
        Approved,
        Rejected,
        Flagged
    }

    /// @dev Compact public anchor. The full claim stays off-chain and can be
    ///      checked against `claimHash` with `verifyClaimData`.
    struct Claim {
        address claimant;
        address insurer;
        address submittedBy;
        bytes32 claimantCommitment;
        bytes32 claimHash;
        string dataPointer;
        Status status;
        uint16 fraudScore;
        uint64 submittedAt;
        uint64 updatedAt;
        bool exists;
    }

    /// @notice One insurer-authorized public intake operation.
    /// @dev Dynamic pointer text is represented by `dataPointerHash` so wallets
    ///      and Solidity sign one unambiguous fixed-width EIP-712 structure.
    struct ClaimPermit {
        address claimant;
        address submitter;
        address insurer;
        bytes32 claimantCommitment;
        bytes32 claimHash;
        bytes32 dataPointerHash;
        bytes32 permitId;
        uint48 deadline;
    }

    /// @dev Coverage decisions are deliberately stored separately from model
    ///      screening. This makes the authority boundary visible in both state
    ///      and events: an assessor contributes a fraud signal, while an
    ///      insurer-scoped decision maker accepts accountability for the final
    ///      coverage outcome and its immutable off-chain audit-record hash.
    struct CoverageDecision {
        bytes32 decisionHash;
        address decidedBy;
        uint64 decidedAt;
    }

    uint256 public claimCount;
    mapping(uint256 claimId => Claim claim) private _claims;
    mapping(address assessor => mapping(address insurer => bool authorized))
        private _assessorScopes;
    mapping(address assessor => uint256 scopeCount) private _assessorScopeCount;
    mapping(address decisionMaker => mapping(address insurer => bool authorized))
        private _decisionMakerScopes;
    mapping(address decisionMaker => uint256 scopeCount)
        private _decisionMakerScopeCount;
    mapping(address issuer => mapping(address insurer => bool authorized))
        private _permitIssuerScopes;
    mapping(address issuer => uint256 scopeCount) private _permitIssuerScopeCount;
    mapping(bytes32 permitId => bool used) private _usedClaimPermits;
    mapping(uint256 claimId => CoverageDecision decision)
        private _coverageDecisions;

    event ClaimSubmitted(
        uint256 indexed claimId,
        address indexed claimant,
        bytes32 claimHash,
        string dataPointer,
        uint256 timestamp
    );
    event ClaimPartiesRecorded(
        uint256 indexed claimId,
        address indexed insurer,
        address indexed submittedBy,
        bytes32 claimantCommitment,
        bytes32 permitId,
        address permitIssuer
    );
    event ClaimAssessed(
        uint256 indexed claimId,
        Status indexed newStatus,
        address indexed assessor,
        uint16 fraudScore,
        uint256 timestamp
    );
    event ClaimDecided(
        uint256 indexed claimId,
        Status indexed newStatus,
        address indexed decisionMaker,
        bytes32 decisionHash,
        uint16 fraudScore,
        uint256 timestamp
    );
    event SubmitterUpdated(address indexed submitter, bool authorized);
    event AssessorUpdated(
        address indexed assessor,
        address indexed insurer,
        bool authorized
    );
    event PermitIssuerUpdated(
        address indexed permitIssuer,
        address indexed insurer,
        bool authorized
    );
    event DecisionMakerUpdated(
        address indexed decisionMaker,
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
    error AssessorScopeMismatch(address assessor, address insurer);
    error DecisionMakerScopeMismatch(address decisionMaker, address insurer);
    error AssessorScopeNotConfigured(address assessor, address insurer);
    error PermitIssuerScopeNotConfigured(
        address permitIssuer,
        address insurer
    );
    error DecisionMakerScopeNotConfigured(
        address decisionMaker,
        address insurer
    );
    error InsurerNotAuthorized(address insurer);
    error EmptyClaimantCommitment();
    error EmptyClaimPermitId();
    error ClaimPermitAlreadyUsed(bytes32 permitId);
    error ClaimPermitExpired(uint48 deadline);
    error ClaimPermitUnauthorized(address permitIssuer, address insurer);
    error ClaimPermitSubmitterMismatch(address expected, address actual);
    error ClaimPermitPointerMismatch();
    error EmptyDecisionHash();
    error RoleSeparationRequired(address account);
    error UseRoleConfigurationFunction(bytes32 role);

    /// @param initialAdmin Account that controls role assignment and starts
    ///        delayed, two-step admin transfers.
    /// @param initialSubmitter Insurer service account permitted to submit.
    /// @param initialPermitIssuer Eligibility signer scoped to initialSubmitter.
    /// @param initialAssessor Scoring account scoped to initialSubmitter.
    /// @param initialDecisionMaker Coverage authority scoped to initialSubmitter.
    /// @param trustedForwarder Immutable ERC-2771 forwarder that verifies
    ///        submitter signatures before restoring their execution context.
    /// @param adminTransferDelay Delay in seconds before an admin transfer can
    ///        be accepted. Production deployments should use a non-zero delay.
    constructor(
        address initialAdmin,
        address initialSubmitter,
        address initialPermitIssuer,
        address initialAssessor,
        address initialDecisionMaker,
        address trustedForwarder,
        uint48 adminTransferDelay
    )
        AccessControlDefaultAdminRules(adminTransferDelay, initialAdmin)
        ERC2771Context(trustedForwarder)
        EIP712("ClaimsRegistry", "2")
    {
        if (
            initialSubmitter == address(0) ||
            initialPermitIssuer == address(0) ||
            initialAssessor == address(0) ||
            initialDecisionMaker == address(0) ||
            trustedForwarder == address(0)
        ) {
            revert ZeroAddress();
        }
        if (
            initialAdmin == initialSubmitter ||
            initialAdmin == initialPermitIssuer ||
            initialAdmin == initialAssessor ||
            initialAdmin == initialDecisionMaker
        ) {
            revert RoleSeparationRequired(initialAdmin);
        }
        if (
            initialSubmitter == initialPermitIssuer ||
            initialSubmitter == initialAssessor ||
            initialSubmitter == initialDecisionMaker ||
            initialPermitIssuer == initialAssessor ||
            initialPermitIssuer == initialDecisionMaker ||
            initialAssessor == initialDecisionMaker
        ) {
            revert RoleSeparationRequired(initialPermitIssuer);
        }

        _grantRole(SUBMITTER_ROLE, initialSubmitter);
        _grantRole(PERMIT_ISSUER_ROLE, initialPermitIssuer);
        _grantRole(ASSESSOR_ROLE, initialAssessor);
        _grantRole(DECISION_MAKER_ROLE, initialDecisionMaker);
        _permitIssuerScopes[initialPermitIssuer][initialSubmitter] = true;
        _permitIssuerScopeCount[initialPermitIssuer] = 1;
        _assessorScopes[initialAssessor][initialSubmitter] = true;
        _assessorScopeCount[initialAssessor] = 1;
        _decisionMakerScopes[initialDecisionMaker][initialSubmitter] = true;
        _decisionMakerScopeCount[initialDecisionMaker] = 1;

        emit SubmitterUpdated(initialSubmitter, true);
        emit PermitIssuerUpdated(
            initialPermitIssuer,
            initialSubmitter,
            true
        );
        emit AssessorUpdated(initialAssessor, initialSubmitter, true);
        emit DecisionMakerUpdated(
            initialDecisionMaker,
            initialSubmitter,
            true
        );
    }

    /// @notice Record an insurer-operated claim during the public-intake migration.
    /// @dev `_msgSender()` preserves the insurer identity for both direct and
    ///      trusted-forwarder calls. New claimant-facing integrations should use
    ///      `submitClaimWithPermit`, which separates all claim parties.
    /// @param claimHash Keccak-256 hash of the canonical off-chain claim bytes.
    /// @param dataPointer Public `ipfs://<CID>` location of the encrypted envelope.
    /// @return claimId Monotonic identifier assigned to the new claim anchor.
    function submitClaim(
        bytes32 claimHash,
        string calldata dataPointer
    ) external onlyRole(SUBMITTER_ROLE) returns (uint256 claimId) {
        address submitter = _msgSender();
        claimId = _recordClaim(
            submitter,
            submitter,
            submitter,
            bytes32(0),
            claimHash,
            dataPointer,
            bytes32(0),
            address(0)
        );
    }

    /// @notice Record a policy-eligible claim signed by a public submitter.
    /// @dev The forwarder signature proves who submitted. The permit signature
    ///      independently proves that an issuer scoped to `permit.insurer`
    ///      approved the exact claimant, content hash, pointer, and deadline.
    ///      A global permit ID makes the insurer proof single-use even when the
    ///      same document is accidentally prepared in two browser sessions.
    /// @param permit Fixed-width claim authorization signed under this contract's
    ///        EIP-712 domain.
    /// @param dataPointer Bare `ipfs://CID` whose hash is bound by the permit.
    /// @param permitSignature Signature produced by an insurer-scoped issuer.
    /// @return claimId Monotonic identifier assigned to the new claim anchor.
    function submitClaimWithPermit(
        ClaimPermit calldata permit,
        string calldata dataPointer,
        bytes calldata permitSignature
    ) external returns (uint256 claimId) {
        address submitter = _msgSender();
        if (
            permit.claimant == address(0) ||
            permit.submitter == address(0) ||
            permit.insurer == address(0)
        ) {
            revert ZeroAddress();
        }
        if (permit.claimantCommitment == bytes32(0)) {
            revert EmptyClaimantCommitment();
        }
        if (permit.claimHash == bytes32(0)) revert EmptyClaimHash();
        if (permit.permitId == bytes32(0)) revert EmptyClaimPermitId();
        if (permit.submitter != submitter) {
            revert ClaimPermitSubmitterMismatch(permit.submitter, submitter);
        }
        if (block.timestamp > permit.deadline) {
            revert ClaimPermitExpired(permit.deadline);
        }
        if (_usedClaimPermits[permit.permitId]) {
            revert ClaimPermitAlreadyUsed(permit.permitId);
        }
        _validateDataPointer(dataPointer);
        if (keccak256(bytes(dataPointer)) != permit.dataPointerHash) {
            revert ClaimPermitPointerMismatch();
        }

        address permitIssuer = ECDSA.recover(
            _hashTypedDataV4(_hashClaimPermit(permit)),
            permitSignature
        );
        if (!isPermitIssuerFor(permitIssuer, permit.insurer)) {
            revert ClaimPermitUnauthorized(permitIssuer, permit.insurer);
        }

        // Mark the authorization before recording the claim. A revert in the
        // remaining call unwinds both changes; a successful reentrant call can
        // never consume the same permit because the used flag is already set.
        _usedClaimPermits[permit.permitId] = true;
        claimId = _recordClaim(
            permit.claimant,
            permit.insurer,
            submitter,
            permit.claimantCommitment,
            permit.claimHash,
            dataPointer,
            permit.permitId,
            permitIssuer
        );
    }

    /// @notice Record the model screening result for a newly submitted claim.
    /// @dev Assessors intentionally cannot approve or reject coverage. Their
    ///      sole authority is to publish an immutable fraud score and move the
    ///      claim into UnderReview or Flagged. A separately scoped decision
    ///      maker must perform the terminal coverage transition.
    /// @param claimId Submitted claim whose screening result will be recorded.
    /// @param newStatus UnderReview or Flagged, based on the screening policy.
    /// @param fraudScore Model score in basis points from 0 through 10,000.
    function assessClaim(
        uint256 claimId,
        Status newStatus,
        uint16 fraudScore
    ) external onlyRole(ASSESSOR_ROLE) {
        Claim storage claim = _claims[claimId];
        address assessor = _msgSender();
        if (!claim.exists) revert UnknownClaim(claimId);
        if (!_assessorScopes[assessor][claim.insurer]) {
            revert AssessorScopeMismatch(assessor, claim.insurer);
        }
        if (fraudScore > 10000) revert InvalidFraudScore(fraudScore);
        if (!_isAllowedAssessmentTransition(claim.status, newStatus)) {
            revert InvalidStatusTransition(claim.status, newStatus);
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

    /// @notice Record the insurer's terminal coverage decision.
    /// @dev `decisionHash` binds the on-chain outcome to an immutable,
    ///      controlled decision audit record held off-chain. The contract does
    ///      not interpret that private record; it enforces that screening
    ///      happened, that the caller is authorized for this insurer, and that
    ///      only a coverage authority can choose Approved or Rejected.
    /// @param claimId Screened claim whose coverage outcome is being finalized.
    /// @param newStatus Approved or Rejected.
    /// @param decisionHash Keccak-256 hash of the canonical decision record.
    function decideClaim(
        uint256 claimId,
        Status newStatus,
        bytes32 decisionHash
    ) external onlyRole(DECISION_MAKER_ROLE) {
        Claim storage claim = _claims[claimId];
        address decisionMaker = _msgSender();
        if (!claim.exists) revert UnknownClaim(claimId);
        if (!_decisionMakerScopes[decisionMaker][claim.insurer]) {
            revert DecisionMakerScopeMismatch(
                decisionMaker,
                claim.insurer
            );
        }
        if (decisionHash == bytes32(0)) revert EmptyDecisionHash();
        if (!_isAllowedDecisionTransition(claim.status, newStatus)) {
            revert InvalidStatusTransition(claim.status, newStatus);
        }

        claim.status = newStatus;
        claim.updatedAt = uint64(block.timestamp);
        _coverageDecisions[claimId] = CoverageDecision({
            decisionHash: decisionHash,
            decidedBy: decisionMaker,
            decidedAt: uint64(block.timestamp)
        });

        emit ClaimDecided(
            claimId,
            newStatus,
            decisionMaker,
            decisionHash,
            claim.fraudScore,
            block.timestamp
        );
    }

    /// @notice Return the current compact public record for one claim.
    /// @dev Reverts for an unknown ID rather than returning an all-zero struct,
    ///      so indexers can distinguish missing data from a legitimate value.
    /// @param claimId Identifier assigned by `submitClaim`.
    /// @return claimant Person represented by this claim. For legacy
    ///         insurer-operated records this remains the insurer address.
    /// @return claimHash Permanent hash of the canonical encrypted envelope.
    /// @return dataPointer Public IPFS pointer supplied at submission.
    /// @return status Current lifecycle state.
    /// @return fraudScore Immutable first-assessment score in basis points.
    /// @return submittedAt Unix timestamp recorded when the claim was created.
    /// @return updatedAt Unix timestamp of the latest lifecycle transition.
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

    /// @notice Return the parties and privacy-preserving claimant reference.
    /// @dev This accessor extends the stable compact `getClaim` interface so
    ///      existing indexers can migrate independently from public intake.
    function getClaimParties(
        uint256 claimId
    )
        external
        view
        returns (
            address insurer,
            address submittedBy,
            bytes32 claimantCommitment
        )
    {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        return (
            claim.insurer,
            claim.submittedBy,
            claim.claimantCommitment
        );
    }

    /// @notice Return the immutable terminal decision audit anchor.
    /// @dev Before a terminal decision all fields are zero. Callers should use
    ///      the claim status to distinguish an undecided record.
    function getClaimDecision(
        uint256 claimId
    )
        external
        view
        returns (
            bytes32 decisionHash,
            address decidedBy,
            uint64 decidedAt
        )
    {
        if (!_claims[claimId].exists) revert UnknownClaim(claimId);
        CoverageDecision storage decision = _coverageDecisions[claimId];
        return (
            decision.decisionHash,
            decision.decidedBy,
            decision.decidedAt
        );
    }

    /// @notice Return the exact EIP-712 digest an issuer must sign.
    /// @dev Exposing this calculation gives off-chain integrations a canonical
    ///      preflight without weakening on-chain verification.
    function claimPermitDigest(
        ClaimPermit calldata permit
    ) external view returns (bytes32) {
        return _hashTypedDataV4(_hashClaimPermit(permit));
    }

    /// @notice Report whether a one-time claim permit has been consumed.
    function isClaimPermitUsed(bytes32 permitId) external view returns (bool) {
        return _usedClaimPermits[permitId];
    }

    /// @notice Compare supplied off-chain bytes with the permanent claim hash.
    /// @param claimId Identifier of the anchor being verified.
    /// @param payload Canonical bytes downloaded from the off-chain pointer.
    /// @return True only when `keccak256(payload)` equals the stored hash.
    function verifyClaimData(
        uint256 claimId,
        bytes calldata payload
    ) external view returns (bool) {
        Claim storage claim = _claims[claimId];
        if (!claim.exists) revert UnknownClaim(claimId);
        return keccak256(payload) == claim.claimHash;
    }

    /// @notice Report whether an account may create new claim anchors.
    /// @param account Address to check against `SUBMITTER_ROLE`.
    /// @return True when the account currently has submission permission.
    function isSubmitter(address account) external view returns (bool) {
        return hasRole(SUBMITTER_ROLE, account);
    }

    /// @notice Report whether an account may assess claims in its insurer scope.
    /// @param account Address to check against `ASSESSOR_ROLE`.
    /// @return True when at least one insurer scope keeps the role active.
    function isAssessor(address account) external view returns (bool) {
        return hasRole(ASSESSOR_ROLE, account);
    }

    /// @notice Report whether an assessor may update one insurer's claims.
    /// @param assessor Scoring account whose scope is being queried.
    /// @param insurer Submitter address that originally anchors the claims.
    /// @return True only when the role and the exact insurer scope are active.
    function isAssessorFor(
        address assessor,
        address insurer
    ) external view returns (bool) {
        return
            hasRole(ASSESSOR_ROLE, assessor) &&
            _assessorScopes[assessor][insurer];
    }

    /// @notice Report whether an account has at least one decision scope.
    function isDecisionMaker(address account) external view returns (bool) {
        return hasRole(DECISION_MAKER_ROLE, account);
    }

    /// @notice Report whether an account may finalize one insurer's claims.
    function isDecisionMakerFor(
        address decisionMaker,
        address insurer
    ) external view returns (bool) {
        return
            hasRole(DECISION_MAKER_ROLE, decisionMaker) &&
            _decisionMakerScopes[decisionMaker][insurer];
    }

    /// @notice Report whether an account may attest eligibility for one insurer.
    /// @dev The global role and exact insurer scope are both required. Revoking
    ///      the insurer's submitter authorization also disables new permits
    ///      without erasing the historical issuer-scope audit trail.
    function isPermitIssuerFor(
        address permitIssuer,
        address insurer
    ) public view returns (bool) {
        return
            hasRole(SUBMITTER_ROLE, insurer) &&
            hasRole(PERMIT_ISSUER_ROLE, permitIssuer) &&
            _permitIssuerScopes[permitIssuer][insurer];
    }

    /// @notice Grant or revoke claim-submission permission.
    /// @dev Admin, submitter, permit-issuer, and assessor duties are mutually
    ///      exclusive. Use this function instead of generic
    ///      `grantRole`/`revokeRole` so that invariant cannot be bypassed.
    /// @param submitter Insurer service account to configure.
    /// @param authorized True to grant permission; false to revoke it.
    function setSubmitter(
        address submitter,
        bool authorized
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (submitter == address(0)) revert ZeroAddress();
        if (authorized) {
            if (
                submitter == defaultAdmin() ||
                hasRole(ASSESSOR_ROLE, submitter) ||
                hasRole(PERMIT_ISSUER_ROLE, submitter) ||
                hasRole(DECISION_MAKER_ROLE, submitter)
            ) {
                revert RoleSeparationRequired(submitter);
            }
            _grantRole(SUBMITTER_ROLE, submitter);
        } else {
            _revokeRole(SUBMITTER_ROLE, submitter);
        }
        emit SubmitterUpdated(submitter, authorized);
    }

    /// @notice Grant or revoke an eligibility signer for one insurer.
    /// @dev Issuers receive the global role only while at least one insurer
    ///      scope exists. The issuer is deliberately separate from the insurer,
    ///      assessor, and administrator so a compromised eligibility key cannot
    ///      administer roles, assess claims, or submit legacy claims directly.
    function setPermitIssuer(
        address permitIssuer,
        address insurer,
        bool authorized
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (permitIssuer == address(0) || insurer == address(0)) {
            revert ZeroAddress();
        }
        if (authorized) {
            if (!hasRole(SUBMITTER_ROLE, insurer)) {
                revert InsurerNotAuthorized(insurer);
            }
            if (
                permitIssuer == defaultAdmin() ||
                hasRole(SUBMITTER_ROLE, permitIssuer) ||
                hasRole(ASSESSOR_ROLE, permitIssuer) ||
                hasRole(DECISION_MAKER_ROLE, permitIssuer)
            ) {
                revert RoleSeparationRequired(permitIssuer);
            }
            if (!_permitIssuerScopes[permitIssuer][insurer]) {
                _permitIssuerScopes[permitIssuer][insurer] = true;
                unchecked {
                    ++_permitIssuerScopeCount[permitIssuer];
                }
                _grantRole(PERMIT_ISSUER_ROLE, permitIssuer);
            }
        } else {
            if (!_permitIssuerScopes[permitIssuer][insurer]) {
                revert PermitIssuerScopeNotConfigured(
                    permitIssuer,
                    insurer
                );
            }
            delete _permitIssuerScopes[permitIssuer][insurer];
            uint256 remainingScopes =
                _permitIssuerScopeCount[permitIssuer] - 1;
            _permitIssuerScopeCount[permitIssuer] = remainingScopes;
            if (remainingScopes == 0) {
                _revokeRole(PERMIT_ISSUER_ROLE, permitIssuer);
            }
        }
        emit PermitIssuerUpdated(permitIssuer, insurer, authorized);
    }

    /// @notice Grant a scoped assessor or revoke its existing scope.
    /// @dev The global assessor role is granted with the first insurer scope and
    ///      revoked after the last scope is removed. Assessment authorization
    ///      therefore always requires both the role and a matching scope entry.
    /// @param assessor Scoring account to configure.
    /// @param insurer Authorized submitter whose claims it may assess.
    /// @param authorized True to add the scope; false to remove it.
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
                hasRole(SUBMITTER_ROLE, assessor) ||
                hasRole(PERMIT_ISSUER_ROLE, assessor) ||
                hasRole(DECISION_MAKER_ROLE, assessor)
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

    /// @notice Grant or revoke terminal coverage authority for one insurer.
    /// @dev The role follows least privilege: it exists only while at least one
    ///      explicit insurer scope exists. Decision makers cannot also submit,
    ///      issue eligibility permits, screen fraud, or administer the registry.
    function setDecisionMaker(
        address decisionMaker,
        address insurer,
        bool authorized
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (decisionMaker == address(0) || insurer == address(0)) {
            revert ZeroAddress();
        }
        if (authorized) {
            if (!hasRole(SUBMITTER_ROLE, insurer)) {
                revert InsurerNotAuthorized(insurer);
            }
            if (
                decisionMaker == defaultAdmin() ||
                hasRole(SUBMITTER_ROLE, decisionMaker) ||
                hasRole(PERMIT_ISSUER_ROLE, decisionMaker) ||
                hasRole(ASSESSOR_ROLE, decisionMaker)
            ) {
                revert RoleSeparationRequired(decisionMaker);
            }
            if (!_decisionMakerScopes[decisionMaker][insurer]) {
                _decisionMakerScopes[decisionMaker][insurer] = true;
                unchecked {
                    ++_decisionMakerScopeCount[decisionMaker];
                }
                _grantRole(DECISION_MAKER_ROLE, decisionMaker);
            }
        } else {
            if (!_decisionMakerScopes[decisionMaker][insurer]) {
                revert DecisionMakerScopeNotConfigured(
                    decisionMaker,
                    insurer
                );
            }
            delete _decisionMakerScopes[decisionMaker][insurer];
            uint256 remainingScopes =
                _decisionMakerScopeCount[decisionMaker] - 1;
            _decisionMakerScopeCount[decisionMaker] = remainingScopes;
            if (remainingScopes == 0) {
                _revokeRole(DECISION_MAKER_ROLE, decisionMaker);
            }
        }
        emit DecisionMakerUpdated(decisionMaker, insurer, authorized);
    }

    /// @notice Start OpenZeppelin's delayed two-step admin transfer.
    /// @dev The proposed admin cannot already be a submitter or assessor. The
    ///      inherited delay and explicit acceptance reduce accidental or
    ///      immediate transfer of the most powerful registry role.
    /// @param newAdmin Proposed admin, or zero to cancel a pending transfer.
    function beginDefaultAdminTransfer(
        address newAdmin
    ) public override onlyRole(DEFAULT_ADMIN_ROLE) {
        if (
            newAdmin != address(0) &&
            (
                hasRole(SUBMITTER_ROLE, newAdmin) ||
                hasRole(ASSESSOR_ROLE, newAdmin) ||
                hasRole(PERMIT_ISSUER_ROLE, newAdmin) ||
                hasRole(DECISION_MAKER_ROLE, newAdmin)
            )
        ) {
            revert RoleSeparationRequired(newAdmin);
        }
        super.beginDefaultAdminTransfer(newAdmin);
    }

    /// @notice Accept admin control after the configured delay has elapsed.
    /// @dev Rechecks role separation at acceptance because the proposed account
    ///      could have received a business role while the delay was pending.
    function acceptDefaultAdminTransfer() public override {
        address sender = _msgSender();
        if (
            hasRole(SUBMITTER_ROLE, sender) ||
            hasRole(ASSESSOR_ROLE, sender) ||
            hasRole(PERMIT_ISSUER_ROLE, sender) ||
            hasRole(DECISION_MAKER_ROLE, sender)
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

    /// @dev Scoped roles must use their invariant-preserving configuration
    ///      functions rather than generic role administration.
    function grantRole(
        bytes32 role,
        address account
    ) public override {
        if (
            role == SUBMITTER_ROLE ||
            role == ASSESSOR_ROLE ||
            role == PERMIT_ISSUER_ROLE ||
            role == DECISION_MAKER_ROLE
        ) {
            revert UseRoleConfigurationFunction(role);
        }
        super.grantRole(role, account);
    }

    /// @dev See grantRole: scoped roles must use the invariant-preserving APIs.
    function revokeRole(
        bytes32 role,
        address account
    ) public override {
        if (
            role == SUBMITTER_ROLE ||
            role == ASSESSOR_ROLE ||
            role == PERMIT_ISSUER_ROLE ||
            role == DECISION_MAKER_ROLE
        ) {
            revert UseRoleConfigurationFunction(role);
        }
        super.revokeRole(role, account);
    }

    /// @dev Concentrate permanent storage and event ordering in one path so
    ///      legacy insurer submissions and permit-backed public submissions
    ///      cannot drift into subtly different claim records.
    function _recordClaim(
        address claimant,
        address insurer,
        address submittedBy,
        bytes32 claimantCommitment,
        bytes32 claimHash,
        string calldata dataPointer,
        bytes32 permitId,
        address permitIssuer
    ) private returns (uint256 claimId) {
        if (claimHash == bytes32(0)) revert EmptyClaimHash();
        _validateDataPointer(dataPointer);

        claimId = claimCount;
        _claims[claimId] = Claim({
            claimant: claimant,
            insurer: insurer,
            submittedBy: submittedBy,
            claimantCommitment: claimantCommitment,
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
            claimant,
            claimHash,
            dataPointer,
            block.timestamp
        );
        emit ClaimPartiesRecorded(
            claimId,
            insurer,
            submittedBy,
            claimantCommitment,
            permitId,
            permitIssuer
        );
    }

    /// @dev Keep the type-hash field ordering next to the Solidity struct. The
    ///      corresponding backend model is tested against `claimPermitDigest`
    ///      so a cross-language field-order change fails before deployment.
    function _hashClaimPermit(
        ClaimPermit calldata permit
    ) private pure returns (bytes32) {
        return
            keccak256(
                abi.encode(
                    CLAIM_PERMIT_TYPEHASH,
                    permit.claimant,
                    permit.submitter,
                    permit.insurer,
                    permit.claimantCommitment,
                    permit.claimHash,
                    permit.dataPointerHash,
                    permit.permitId,
                    permit.deadline
                )
            );
    }

    /// @dev Screening is a one-time contribution. It cannot be rewritten by an
    ///      assessor after another party has reviewed the model output.
    function _isAllowedAssessmentTransition(
        Status currentStatus,
        Status requestedStatus
    ) private pure returns (bool) {
        return
            currentStatus == Status.Submitted &&
            (
                requestedStatus == Status.UnderReview ||
                requestedStatus == Status.Flagged
            );
    }

    /// @dev Only reviewed claims can reach a terminal coverage state. Terminal
    ///      states deliberately have no outgoing transitions.
    function _isAllowedDecisionTransition(
        Status currentStatus,
        Status requestedStatus
    ) private pure returns (bool) {
        bool isScreened =
            currentStatus == Status.UnderReview ||
            currentStatus == Status.Flagged;
        bool isTerminal =
            requestedStatus == Status.Approved ||
            requestedStatus == Status.Rejected;
        return isScreened && isTerminal;
    }

    /// @dev This deployment stores a bare IPFS CID only. The application layer
    ///      must ensure it identifies an authenticated encrypted envelope;
    ///      Solidity can validate the pointer syntax and content hash, but it
    ///      cannot prove that arbitrary bytes were encrypted. Requiring an
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
