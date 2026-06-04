// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from
    "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {PausableUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IVault} from "./interfaces/IVault.sol";

/// @title Vault (Perps Agent L2)
/// @author Perps Agent
/// @notice Holds a performance bond + settles fees on-chain (mETH / USDe / USDC).
///         Trading capital stays on the user's CEX account; this vault NEVER
///         bridges to the CEX (honest custody boundary, docs/CONCEPT.md §7, §11).
/// @dev    UUPS-upgradeable, ERC-7201 storage. Checks-Effects-Interactions +
///         a self-contained reentrancy guard (OZ 5.6 ships only the transient
///         variant; we keep the guard in namespaced storage to avoid any
///         dependency on EIP-1153) + SafeERC20. Deposits credit the *received*
///         amount so fee-on-transfer tokens cannot over-credit. Withdrawals stay
///         available even when paused, so a pause can never trap user funds.
/// @custom:security-contact security@perpsagent.example
contract Vault is
    Initializable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    UUPSUpgradeable,
    IVault
{
    using SafeERC20 for IERC20;

    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant FEE_MANAGER_ROLE = keccak256("FEE_MANAGER_ROLE");

    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;

    /// @custom:storage-location erc7201:perpsagent.storage.Vault
    struct VaultStorage {
        mapping(address user => mapping(address asset => uint256)) balances;
        address treasury;
        uint256 reentrancyStatus;
    }

    // keccak256(abi.encode(uint256(keccak256("perpsagent.storage.Vault")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant _STORAGE =
        0xd8c297901d5c41b268f0559fdce247e5db4000f30ecedabb33fc17510acd6d00;

    error ZeroAddress();
    error ZeroAmount();
    error InsufficientBalance(uint256 available, uint256 requested);
    error NothingReceived();
    error ReentrancyGuardReentrantCall();

    function _s() private pure returns (VaultStorage storage $) {
        assembly {
            $.slot := _STORAGE
        }
    }

    /// @dev Mirrors OpenZeppelin's storage-based ReentrancyGuard, kept in this
    ///      contract's ERC-7201 namespace so it is upgrade-safe and needs no
    ///      transient-storage (EIP-1153) support from the chain.
    modifier nonReentrant() {
        VaultStorage storage $ = _s();
        if ($.reentrancyStatus == _ENTERED) revert ReentrancyGuardReentrantCall();
        $.reentrancyStatus = _ENTERED;
        _;
        $.reentrancyStatus = _NOT_ENTERED;
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address admin, address treasury_) external initializer {
        if (admin == address(0) || treasury_ == address(0)) revert ZeroAddress();
        __AccessControl_init();
        __Pausable_init();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(UPGRADER_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
        _grantRole(FEE_MANAGER_ROLE, admin);
        VaultStorage storage $ = _s();
        $.treasury = treasury_;
        $.reentrancyStatus = _NOT_ENTERED;
        emit TreasuryUpdated(address(0), treasury_);
    }

    /// @inheritdoc IVault
    function deposit(address asset, uint256 amount) external nonReentrant whenNotPaused {
        if (asset == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        IERC20 token = IERC20(asset);
        uint256 balBefore = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), amount);
        uint256 received = token.balanceOf(address(this)) - balBefore;
        if (received == 0) revert NothingReceived();
        _s().balances[msg.sender][asset] += received;
        emit Deposited(msg.sender, asset, amount, received);
    }

    /// @inheritdoc IVault
    /// @dev Intentionally callable while paused so users can always exit.
    function withdraw(address asset, uint256 amount) external nonReentrant {
        if (amount == 0) revert ZeroAmount();
        VaultStorage storage $ = _s();
        uint256 bal = $.balances[msg.sender][asset];
        if (bal < amount) revert InsufficientBalance(bal, amount);
        $.balances[msg.sender][asset] = bal - amount;
        IERC20(asset).safeTransfer(msg.sender, amount);
        emit Withdrawn(msg.sender, asset, amount);
    }

    /// @inheritdoc IVault
    /// @notice Move an accrued performance fee from a user's balance to the treasury.
    function settleFee(address user, address asset, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        onlyRole(FEE_MANAGER_ROLE)
    {
        if (amount == 0) revert ZeroAmount();
        VaultStorage storage $ = _s();
        uint256 bal = $.balances[user][asset];
        if (bal < amount) revert InsufficientBalance(bal, amount);
        $.balances[user][asset] = bal - amount;
        $.balances[$.treasury][asset] += amount;
        emit FeeSettled(user, asset, amount, $.treasury);
    }

    function setTreasury(address newTreasury) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newTreasury == address(0)) revert ZeroAddress();
        VaultStorage storage $ = _s();
        emit TreasuryUpdated($.treasury, newTreasury);
        $.treasury = newTreasury;
    }

    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    /// @inheritdoc IVault
    function balanceOf(address user, address asset) external view returns (uint256) {
        return _s().balances[user][asset];
    }

    function treasury() external view returns (address) {
        return _s().treasury;
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyRole(UPGRADER_ROLE) {}
}
