// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {Upgrades} from "openzeppelin-foundry-upgrades/Upgrades.sol";

import {StrategyLedger} from "../src/StrategyLedger.sol";
import {StrategyMemory} from "../src/StrategyMemory.sol";
import {Vault} from "../src/Vault.sol";

/// @notice Deploys UUPS proxies for the three Perps Agent contracts on Mantle.
/// @dev Requires node + ffi (see foundry.toml). Run `forge clean` first.
///      Usage:
///        forge script script/Deploy.s.sol:Deploy \
///          --rpc-url mantle_testnet --broadcast --verify --force
contract Deploy is Script {
    function run() external {
        address admin = vm.envOr("PERPSAGENT_ADMIN", msg.sender);
        address treasury = vm.envOr("PERPSAGENT_TREASURY", admin);

        vm.startBroadcast();

        address ledger =
            Upgrades.deployUUPSProxy("StrategyLedger.sol", abi.encodeCall(StrategyLedger.initialize, (admin)));

        address strategyMemory =
            Upgrades.deployUUPSProxy("StrategyMemory.sol", abi.encodeCall(StrategyMemory.initialize, (admin)));

        address vault = Upgrades.deployUUPSProxy(
            "Vault.sol", abi.encodeCall(Vault.initialize, (admin, treasury))
        );

        vm.stopBroadcast();

        console.log("StrategyLedger proxy:", ledger);
        console.log("StrategyMemory proxy:", strategyMemory);
        console.log("Vault proxy:        ", vault);
    }
}
