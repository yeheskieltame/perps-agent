// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IStrategyMemory
/// @notice Append-only public experience buffer: regime -> params -> verified
///         outcome. Read back by the agent (recall) to choose params and learn.
interface IStrategyMemory {
    struct Record {
        bytes32 regimeKey; // bucketed regime fingerprint (k-NN key)
        bytes32 configHash;
        int256 realizedPnl;
        uint32 winrateBps; // 0..10_000
        int32 riskAdjBps; // signed ranking score: better systems, not max PnL
        bool isBacktest;
        address agent;
        uint64 timestamp;
    }

    event MemoryWritten(
        bytes32 indexed regimeKey,
        address indexed agent,
        uint256 indexed index,
        bytes32 configHash,
        int32 riskAdjBps,
        bool isBacktest
    );

    function write(
        bytes32 regimeKey,
        bytes32 configHash,
        int256 realizedPnl,
        uint32 winrateBps,
        int32 riskAdjBps,
        bool isBacktest
    ) external returns (uint256 index);

    function countByRegime(bytes32 regimeKey) external view returns (uint256);
    function getByRegime(bytes32 regimeKey, uint256 offset, uint256 limit)
        external
        view
        returns (Record[] memory);
    function totalRecords() external view returns (uint256);
}
