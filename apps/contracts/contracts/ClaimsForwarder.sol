// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {
    ERC2771Forwarder
} from "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";

/// @title ClaimsForwarder
/// @notice Immutable EIP-712 verifier and ERC-2771 execution gateway for the
///         claims registry. Sponsorship policy remains off-chain: anyone may
///         pay to relay a valid insurer-signed request.
/// @dev OpenZeppelin maintains one nonce per signer and verifies the signature,
///      deadline, target, value, gas allowance, and calldata before forwarding.
///      This contract intentionally has no owner or withdrawal mechanism; it is
///      an authorization verifier, not a treasury or role-management service.
contract ClaimsForwarder is ERC2771Forwarder {
    string public constant FORWARDER_NAME = "ClaimsRegistryForwarder";

    /// @dev OpenZeppelin derives the EIP-712 domain from this immutable name and
    ///      version 1. The off-chain relayer decides which valid requests it
    ///      pays for, but cannot change what the insurer authorized.
    constructor() ERC2771Forwarder(FORWARDER_NAME) {}
}
