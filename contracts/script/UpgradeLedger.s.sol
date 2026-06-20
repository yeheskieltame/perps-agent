// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Script, console2} from "forge-std/Script.sol";

import {StrategyLedger} from "../src/StrategyLedger.sol";

/// @title UpgradeLedger
/// @notice Deploy a new StrategyLedger implementation and point the existing UUPS
///         proxy at it. The storage change is additive (an `attestationHistory`
///         mapping appended to the ERC-7201 struct), so the layout is preserved and
///         the upgrade is safe. Must be run by an UPGRADER_ROLE holder (the deployer).
/// @dev    LEDGER_PROXY env = the proxy address. Example:
///           LEDGER_PROXY=0x128E... forge script script/UpgradeLedger.s.sol:UpgradeLedger \
///             --rpc-url "$RPC" --broadcast --private-key "$PRIVATE_KEY"
contract UpgradeLedger is Script {
    function run() external {
        address proxy = vm.envAddress("LEDGER_PROXY");
        vm.startBroadcast();
        StrategyLedger impl = new StrategyLedger();
        StrategyLedger(proxy).upgradeToAndCall(address(impl), "");
        vm.stopBroadcast();
        console2.log("StrategyLedger proxy:", proxy);
        console2.log("new implementation: ", address(impl));
    }
}
