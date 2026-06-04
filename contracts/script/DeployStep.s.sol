// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {Upgrades} from "openzeppelin-foundry-upgrades/Upgrades.sol";

import {StrategyLedger} from "../src/StrategyLedger.sol";
import {StrategyMemory} from "../src/StrategyMemory.sol";
import {Vault} from "../src/Vault.sol";

/// @notice One validated UUPS deploy per run (WHICH=1|2|3) so each step fits a
///         short execution window. Same Upgrades safety checks as Deploy.s.sol.
contract DeployStep is Script {
    function run() external {
        uint256 which = vm.envUint("WHICH");
        address admin = vm.envOr("PERPSAGENT_ADMIN", msg.sender);
        address treasury = vm.envOr("PERPSAGENT_TREASURY", admin);
        vm.startBroadcast();
        if (which == 1) {
            console.log("StrategyLedger proxy:", Upgrades.deployUUPSProxy(
                "StrategyLedger.sol", abi.encodeCall(StrategyLedger.initialize, (admin))));
        } else if (which == 2) {
            console.log("StrategyMemory proxy:", Upgrades.deployUUPSProxy(
                "StrategyMemory.sol", abi.encodeCall(StrategyMemory.initialize, (admin))));
        } else if (which == 3) {
            console.log("Vault proxy:", Upgrades.deployUUPSProxy(
                "Vault.sol", abi.encodeCall(Vault.initialize, (admin, treasury))));
        }
        vm.stopBroadcast();
    }
}
