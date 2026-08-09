// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {
    ERC2771Forwarder
} from "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";

/// @title ClaimsForwarder
/// @notice Immutable EIP-712 verifier and ERC-2771 execution gateway for the
///         claims registry. Sponsorship policy remains off-chain: anyone may
///         pay to relay a valid insurer-signed request.
contract ClaimsForwarder is ERC2771Forwarder {
    string public constant FORWARDER_NAME = "ClaimsRegistryForwarder";

    /// @dev OpenZeppelin derives the EIP-712 domain from this immutable name and
    ///      version 1. The contract intentionally adds no mutable sponsorship
    ///      policy; the off-chain relayer decides which valid requests it pays.
    constructor() ERC2771Forwarder(FORWARDER_NAME) {}
}
