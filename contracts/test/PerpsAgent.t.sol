// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import {Test} from "forge-std/Test.sol";
import {UnsafeUpgrades} from "openzeppelin-foundry-upgrades/Upgrades.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

import {StrategyLedger} from "../src/StrategyLedger.sol";
import {IStrategyLedger} from "../src/interfaces/IStrategyLedger.sol";
import {StrategyMemory} from "../src/StrategyMemory.sol";
import {IStrategyMemory} from "../src/interfaces/IStrategyMemory.sol";
import {Vault} from "../src/Vault.sol";

contract MockERC20 is ERC20 {
    constructor() ERC20("Mock", "MOCK") {
        _mint(msg.sender, 1_000_000e18);
    }
}

contract StrategyLedgerTest is Test {
    StrategyLedger ledger;
    address admin = address(this);
    address agent = address(0xA11CE);

    function setUp() public {
        address proxy = UnsafeUpgrades.deployUUPSProxy(
            address(new StrategyLedger()), abi.encodeCall(StrategyLedger.initialize, (admin))
        );
        ledger = StrategyLedger(proxy);
    }

    function test_commit_then_attest() public {
        bytes32 id = keccak256("instance-1");
        bytes32 cfg = keccak256("config");
        vm.prank(agent);
        ledger.commitStrategy(id, cfg);

        IStrategyLedger.Commitment memory c = ledger.getCommitment(id);
        assertEq(c.agent, agent);
        assertEq(c.configHash, cfg);

        vm.prank(agent);
        ledger.attest(id, int256(1234), 6800, int32(15000), keccak256("fills"));
        IStrategyLedger.Attestation memory a = ledger.getLatestAttestation(id);
        assertEq(a.winrateBps, 6800);
        assertEq(a.count, 1);
    }

    function test_attest_requires_committing_agent() public {
        bytes32 id = keccak256("i2");
        vm.prank(agent);
        ledger.commitStrategy(id, keccak256("c"));
        vm.expectRevert(); // caller != agent
        ledger.attest(id, 0, 100, 0, bytes32(0));
    }

    function test_double_commit_reverts() public {
        bytes32 id = keccak256("i3");
        vm.startPrank(agent);
        ledger.commitStrategy(id, keccak256("c"));
        vm.expectRevert();
        ledger.commitStrategy(id, keccak256("c2"));
        vm.stopPrank();
    }

    function test_winrate_bound() public {
        bytes32 id = keccak256("i4");
        vm.startPrank(agent);
        ledger.commitStrategy(id, keccak256("c"));
        vm.expectRevert();
        ledger.attest(id, 0, 10001, 0, bytes32(0));
        vm.stopPrank();
    }
}

contract StrategyMemoryTest is Test {
    StrategyMemory mem;

    function setUp() public {
        address proxy = UnsafeUpgrades.deployUUPSProxy(
            address(new StrategyMemory()), abi.encodeCall(StrategyMemory.initialize, (address(this)))
        );
        mem = StrategyMemory(proxy);
    }

    function test_write_and_recall() public {
        bytes32 rk = keccak256("regime-A");
        mem.write(rk, keccak256("cfg1"), int256(100), 5500, int32(12000), true);
        mem.write(rk, keccak256("cfg2"), int256(200), 6000, int32(18000), false);

        assertEq(mem.countByRegime(rk), 2);
        assertEq(mem.totalRecords(), 2);

        IStrategyMemory.Record[] memory recs = mem.getByRegime(rk, 0, 10);
        assertEq(recs.length, 2);
        assertEq(recs[0].agent, address(this));
        assertEq(recs[1].winrateBps, 6000);
    }

    function test_pagination_out_of_range_is_empty() public {
        bytes32 rk = keccak256("regime-B");
        mem.write(rk, bytes32(0), 0, 0, 0, false);
        IStrategyMemory.Record[] memory recs = mem.getByRegime(rk, 5, 10);
        assertEq(recs.length, 0);
    }
}

contract VaultTest is Test {
    Vault vault;
    MockERC20 token;
    address user = address(0xB0B);
    address treasury = address(0x7);

    function setUp() public {
        token = new MockERC20();
        address proxy = UnsafeUpgrades.deployUUPSProxy(
            address(new Vault()), abi.encodeCall(Vault.initialize, (address(this), treasury))
        );
        vault = Vault(proxy);
        token.transfer(user, 1_000e18);
    }

    function test_deposit_withdraw() public {
        vm.startPrank(user);
        token.approve(address(vault), 500e18);
        vault.deposit(address(token), 500e18);
        assertEq(vault.balanceOf(user, address(token)), 500e18);
        vault.withdraw(address(token), 200e18);
        assertEq(vault.balanceOf(user, address(token)), 300e18);
        vm.stopPrank();
        assertEq(token.balanceOf(user), 700e18);
    }

    function test_settleFee_moves_to_treasury() public {
        vm.startPrank(user);
        token.approve(address(vault), 500e18);
        vault.deposit(address(token), 500e18);
        vm.stopPrank();

        vault.settleFee(user, address(token), 50e18); // address(this) holds FEE_MANAGER_ROLE
        assertEq(vault.balanceOf(user, address(token)), 450e18);
        assertEq(vault.balanceOf(treasury, address(token)), 50e18);
    }

    function test_withdraw_works_while_paused() public {
        vm.startPrank(user);
        token.approve(address(vault), 100e18);
        vault.deposit(address(token), 100e18);
        vm.stopPrank();

        vault.pause(); // address(this) holds PAUSER_ROLE
        vm.prank(user);
        vault.withdraw(address(token), 100e18); // must still succeed
        assertEq(vault.balanceOf(user, address(token)), 0);
    }
}
