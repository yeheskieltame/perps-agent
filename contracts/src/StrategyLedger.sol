// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from
    "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";

import {IStrategyLedger} from "./interfaces/IStrategyLedger.sol";

/// @title StrategyLedger (Perps Agent L1)
/// @author Perps Agent
/// @notice Pre-commitment + verified attestation of grid strategies. Commit the
///         config hash BEFORE trading so parameters cannot be retrofitted to
///         results; attest verified outcomes after. UUPS-upgradeable.
/// @dev    Uses ERC-7201 namespaced storage to remain upgrade-safe. See
///         docs.perpsagent.xyz Only the committing agent may attest its instance.
/// @custom:security-contact security@perpsagent.example
contract StrategyLedger is Initializable, AccessControlUpgradeable, UUPSUpgradeable, IStrategyLedger {
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    uint32 private constant _MAX_BPS = 10_000;

    /// @custom:storage-location erc7201:perpsagent.storage.StrategyLedger
    struct LedgerStorage {
        mapping(bytes32 instanceId => Commitment) commitments;
        mapping(bytes32 instanceId => Attestation) attestations;
    }

    // keccak256(abi.encode(uint256(keccak256("perpsagent.storage.StrategyLedger")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant _STORAGE =
        0xb87589f05add5f69ec02a2e0fe6c3f9f6057d65c0dcf2fb44ed64676ad719200;

    error ZeroAddress();
    error ZeroInstanceId();
    error ZeroConfigHash();
    error AlreadyCommitted(bytes32 instanceId);
    error NotCommitted(bytes32 instanceId);
    error NotStrategyAgent(bytes32 instanceId, address caller);
    error InvalidBps(uint32 value);

    function _s() private pure returns (LedgerStorage storage $) {
        assembly {
            $.slot := _STORAGE
        }
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /// @notice Initialize the proxy. `admin` receives DEFAULT_ADMIN_ROLE + UPGRADER_ROLE.
    function initialize(address admin) external initializer {
        if (admin == address(0)) revert ZeroAddress();
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(UPGRADER_ROLE, admin);
    }

    /// @inheritdoc IStrategyLedger
    function commitStrategy(bytes32 instanceId, bytes32 configHash) external {
        if (instanceId == bytes32(0)) revert ZeroInstanceId();
        if (configHash == bytes32(0)) revert ZeroConfigHash();
        LedgerStorage storage $ = _s();
        if ($.commitments[instanceId].agent != address(0)) revert AlreadyCommitted(instanceId);
        $.commitments[instanceId] =
            Commitment({agent: msg.sender, configHash: configHash, committedAt: uint64(block.timestamp)});
        emit StrategyCommitted(instanceId, msg.sender, configHash, uint64(block.timestamp));
    }

    /// @inheritdoc IStrategyLedger
    function attest(
        bytes32 instanceId,
        int256 realizedPnl,
        uint32 winrateBps,
        int32 riskAdjBps,
        bytes32 fillsRoot
    ) external {
        if (winrateBps > _MAX_BPS) revert InvalidBps(winrateBps);
        LedgerStorage storage $ = _s();
        Commitment storage c = $.commitments[instanceId];
        if (c.agent == address(0)) revert NotCommitted(instanceId);
        if (c.agent != msg.sender) revert NotStrategyAgent(instanceId, msg.sender);

        Attestation storage a = $.attestations[instanceId];
        a.realizedPnl = realizedPnl;
        a.winrateBps = winrateBps;
        a.riskAdjBps = riskAdjBps;
        a.fillsRoot = fillsRoot;
        a.attestedAt = uint64(block.timestamp);
        unchecked {
            a.count += 1;
        }
        emit OutcomeAttested(
            instanceId, msg.sender, realizedPnl, winrateBps, riskAdjBps, fillsRoot, uint64(block.timestamp)
        );
    }

    /// @inheritdoc IStrategyLedger
    function getCommitment(bytes32 instanceId) external view returns (Commitment memory) {
        return _s().commitments[instanceId];
    }

    /// @inheritdoc IStrategyLedger
    function getLatestAttestation(bytes32 instanceId) external view returns (Attestation memory) {
        return _s().attestations[instanceId];
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyRole(UPGRADER_ROLE) {}
}
