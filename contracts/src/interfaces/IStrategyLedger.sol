// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IStrategyLedger
/// @notice Pre-commitment + verified attestation of grid strategies (Perps Agent L1).
interface IStrategyLedger {
    struct Commitment {
        address agent;
        bytes32 configHash;
        uint64 committedAt;
    }

    struct Attestation {
        int256 realizedPnl;
        uint32 winrateBps; // 0..10_000
        int32 riskAdjBps; // signed: a losing system is negative
        bytes32 fillsRoot; // Merkle root over the fill log
        uint64 attestedAt;
        uint32 count;
    }

    event StrategyCommitted(
        bytes32 indexed instanceId, address indexed agent, bytes32 configHash, uint64 timestamp
    );
    event OutcomeAttested(
        bytes32 indexed instanceId,
        address indexed agent,
        int256 realizedPnl,
        uint32 winrateBps,
        int32 riskAdjBps,
        bytes32 fillsRoot,
        uint64 timestamp
    );

    function commitStrategy(bytes32 instanceId, bytes32 configHash) external;
    function attest(
        bytes32 instanceId,
        int256 realizedPnl,
        uint32 winrateBps,
        int32 riskAdjBps,
        bytes32 fillsRoot
    ) external;

    function getCommitment(bytes32 instanceId) external view returns (Commitment memory);
    function getLatestAttestation(bytes32 instanceId) external view returns (Attestation memory);
    /// @notice Number of attestations recorded for an instance (append-only history).
    function getAttestationCount(bytes32 instanceId) external view returns (uint256);
    /// @notice The i-th attestation in the append-only history (0 = first).
    function getAttestationAt(bytes32 instanceId, uint256 index) external view returns (Attestation memory);
}
