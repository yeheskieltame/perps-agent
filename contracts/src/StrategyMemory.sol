// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from
    "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";

import {IStrategyMemory} from "./interfaces/IStrategyMemory.sol";

/// @title StrategyMemory (Perps Agent L1)
/// @author Perps Agent
/// @notice Append-only, public experience buffer: regime -> params -> verified
///         outcome. Read back by the agent (recall) to choose parameters and to
///         learn. This is what makes Mantle the strategy *brain*, not just an
///         audit log (docs/CONCEPT.md §4). UUPS-upgradeable.
/// @dev    `agent` and `timestamp` are set from msg.sender / block to prevent
///         spoofing. Records are append-only; there is no delete.
/// @custom:security-contact security@perpsagent.example
contract StrategyMemory is Initializable, AccessControlUpgradeable, UUPSUpgradeable, IStrategyMemory {
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    uint32 private constant _MAX_BPS = 10_000;
    uint256 private constant _MAX_PAGE = 200;

    /// @custom:storage-location erc7201:perpsagent.storage.StrategyMemory
    struct MemoryStorage {
        mapping(bytes32 regimeKey => Record[]) byRegime;
        uint256 total;
    }

    // keccak256(abi.encode(uint256(keccak256("perpsagent.storage.StrategyMemory")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant _STORAGE =
        0xc2862da2353c7ec461921a1cfa2b4782beeb8e518b225cf1468a34b30c5dfd00;

    error ZeroAddress();
    error ZeroRegimeKey();
    error InvalidBps(uint32 value);
    error PageTooLarge(uint256 limit);

    function _s() private pure returns (MemoryStorage storage $) {
        assembly {
            $.slot := _STORAGE
        }
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address admin) external initializer {
        if (admin == address(0)) revert ZeroAddress();
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(UPGRADER_ROLE, admin);
    }

    /// @inheritdoc IStrategyMemory
    function write(
        bytes32 regimeKey,
        bytes32 configHash,
        int256 realizedPnl,
        uint32 winrateBps,
        int32 riskAdjBps,
        bool isBacktest
    ) external returns (uint256 index) {
        if (regimeKey == bytes32(0)) revert ZeroRegimeKey();
        if (winrateBps > _MAX_BPS) revert InvalidBps(winrateBps);

        MemoryStorage storage $ = _s();
        Record[] storage recs = $.byRegime[regimeKey];
        index = recs.length;
        recs.push(
            Record({
                regimeKey: regimeKey,
                configHash: configHash,
                realizedPnl: realizedPnl,
                winrateBps: winrateBps,
                riskAdjBps: riskAdjBps,
                isBacktest: isBacktest,
                agent: msg.sender,
                timestamp: uint64(block.timestamp)
            })
        );
        unchecked {
            $.total += 1;
        }
        emit MemoryWritten(regimeKey, msg.sender, index, configHash, riskAdjBps, isBacktest);
    }

    /// @inheritdoc IStrategyMemory
    function countByRegime(bytes32 regimeKey) external view returns (uint256) {
        return _s().byRegime[regimeKey].length;
    }

    /// @inheritdoc IStrategyMemory
    function getByRegime(bytes32 regimeKey, uint256 offset, uint256 limit)
        external
        view
        returns (Record[] memory out)
    {
        if (limit > _MAX_PAGE) revert PageTooLarge(limit);
        Record[] storage recs = _s().byRegime[regimeKey];
        uint256 len = recs.length;
        if (offset >= len) return new Record[](0);
        uint256 end = offset + limit;
        if (end > len) end = len;
        out = new Record[](end - offset);
        for (uint256 i = offset; i < end; ++i) {
            out[i - offset] = recs[i];
        }
    }

    /// @inheritdoc IStrategyMemory
    function totalRecords() external view returns (uint256) {
        return _s().total;
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyRole(UPGRADER_ROLE) {}
}
