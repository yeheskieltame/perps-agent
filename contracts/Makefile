.PHONY: install build test deploy-testnet clean

install:
	forge install foundry-rs/forge-std
	forge install OpenZeppelin/openzeppelin-foundry-upgrades
	forge install OpenZeppelin/openzeppelin-contracts-upgradeable

build:
	forge clean && forge build

test:
	forge clean && forge test -vvv

deploy-testnet:
	forge clean && forge script script/Deploy.s.sol:Deploy --rpc-url mantle_testnet --broadcast --verify --force

clean:
	forge clean
