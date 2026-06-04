// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {UnsafeUpgrades} from "openzeppelin-foundry-upgrades/Upgrades.sol";

import {StrategyLedger} from "../src/StrategyLedger.sol";
import {StrategyMemory} from "../src/StrategyMemory.sol";
import {Vault} from "../src/Vault.sol";

/// @notice Fast local/anvil deploy (no ffi/upgrade-safety validations) for tests
/// and integration. For testnet/mainnet use Deploy.s.sol (runs full validations).
contract DeployLocal is Script {
    function run() external {
        address admin = vm.envOr("PERPSAGENT_ADMIN", msg.sender);
        address treasury = vm.envOr("PERPSAGENT_TREASURY", admin);
        vm.startBroadcast();
        address ledger = UnsafeUpgrades.deployUUPSProxy(
            address(new StrategyLedger()), abi.encodeCall(StrategyLedger.initialize, (admin)));
        address strategyMemory = UnsafeUpgrades.deployUUPSProxy(
            address(new StrategyMemory()), abi.encodeCall(StrategyMemory.initialize, (admin)));
        address vault = UnsafeUpgrades.deployUUPSProxy(
            address(new Vault()), abi.encodeCall(Vault.initialize, (admin, treasury)));
        vm.stopBroadcast();
        console.log("StrategyLedger proxy:", ledger);
        console.log("StrategyMemory proxy:", strategyMemory);
        console.log("Vault proxy:        ", vault);
    }
}
