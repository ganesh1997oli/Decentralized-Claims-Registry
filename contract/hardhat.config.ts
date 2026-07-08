import hardhatToolboxViemPlugin from "@nomicfoundation/hardhat-toolbox-viem";
import { defineConfig } from "hardhat/config";

export default defineConfig({
  plugins: [hardhatToolboxViemPlugin],
  solidity: {
    profiles: {
      default: {
        version: "0.8.28",
      },
      production: {
        version: "0.8.28",
        settings: {
          optimizer: {
            enabled: true,
            runs: 200,
          },
        },
      },
    },
  },
  networks: {
    hardhatMainnet: {
      type: "edr-simulated",
      chainType: "l1",
    },
    hardhatOp: {
      type: "edr-simulated",
      chainType: "op",
    },
    sepolia: {
      type: "http",
      chainType: "l1",
      // url: configVariable("SEPOLIA_RPC_URL"),
      url: "https://ethereum-sepolia-rpc.publicnode.com",
      // accounts: [configVariable("SEPOLIA_PRIVATE_KEY")],
      accounts: ["0x302958f35b98d20470f7da846f2fc2b22c3493d9f5420bc0d309494c8ba6fa36"]
    },
  },
});
