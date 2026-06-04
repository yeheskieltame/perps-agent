// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IVault
/// @notice Non-custodial-to-platform vault: holds a performance bond and settles
///         fees on-chain. Trading capital stays on the user's CEX account.
interface IVault {
    event Deposited(address indexed user, address indexed asset, uint256 amount, uint256 credited);
    event Withdrawn(address indexed user, address indexed asset, uint256 amount);
    event FeeSettled(
        address indexed user, address indexed asset, uint256 amount, address indexed treasury
    );
    event TreasuryUpdated(address indexed previousTreasury, address indexed newTreasury);

    function deposit(address asset, uint256 amount) external;
    function withdraw(address asset, uint256 amount) external;
    function settleFee(address user, address asset, uint256 amount) external;
    function balanceOf(address user, address asset) external view returns (uint256);
}
