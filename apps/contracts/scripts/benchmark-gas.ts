import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { network } from "hardhat";
import { encodeFunctionData, keccak256, toHex } from "viem";

const requested = Number.parseInt(
  process.env.BENCHMARK_GAS_TRANSACTIONS ?? "100",
  10,
);
if (!Number.isSafeInteger(requested) || requested < 1 || requested > 10_000) {
  throw new Error("BENCHMARK_GAS_TRANSACTIONS must be between 1 and 10000");
}

const output = resolve(
  process.env.BENCHMARK_GAS_OUTPUT ??
    "../../benchmarks/local/results/gas-results.json",
);
const { viem } = await network.create();
const [
  admin,
  insurer,
  permitIssuer,
  assessor,
  claimant,
  relayer,
] = await viem.getWalletClients();
const publicClient = await viem.getPublicClient();
const chainId = await publicClient.getChainId();

const forwarder = await viem.deployContract("ClaimsForwarder");
const registry = await viem.deployContract("ClaimsRegistry", [
  admin.account.address,
  insurer.account.address,
  permitIssuer.account.address,
  assessor.account.address,
  forwarder.address,
  0,
]);

type GasObservation = {
  operation: "forwarded_public_submission" | "assessment";
  iteration: number;
  transaction_hash: `0x${string}`;
  block_number: string;
  gas_used: string;
  status?: "UnderReview" | "Flagged";
};

const observations: GasObservation[] = [];
// The first transaction warms contract/account state and is excluded from the
// retained sample. Every retained submission still writes a new claim and
// one-time permit, matching the application path.
for (let iteration = 0; iteration <= requested; iteration += 1) {
  const latestBlock = await publicClient.getBlock();
  const dataPointer = `ipfs://benchmarkclaim${iteration}`;
  const claimHash = keccak256(toHex(`benchmark-claim-${iteration}`));
  const claimantCommitment = keccak256(
    toHex(`benchmark-claimant-${iteration}`),
  );
  const permitId = keccak256(toHex(`benchmark-permit-${iteration}`));
  const permit = {
    claimant: claimant.account.address,
    submitter: claimant.account.address,
    insurer: insurer.account.address,
    claimantCommitment,
    claimHash,
    dataPointerHash: keccak256(toHex(dataPointer)),
    permitId,
    deadline: latestBlock.timestamp + 3_600n,
  } as const;
  const permitSignature = await permitIssuer.signTypedData({
    account: permitIssuer.account,
    domain: {
      name: "ClaimsRegistry",
      version: "2",
      chainId,
      verifyingContract: registry.address,
    },
    types: {
      ClaimPermit: [
        { name: "claimant", type: "address" },
        { name: "submitter", type: "address" },
        { name: "insurer", type: "address" },
        { name: "claimantCommitment", type: "bytes32" },
        { name: "claimHash", type: "bytes32" },
        { name: "dataPointerHash", type: "bytes32" },
        { name: "permitId", type: "bytes32" },
        { name: "deadline", type: "uint48" },
      ],
    },
    primaryType: "ClaimPermit",
    message: permit,
  });
  const nonce = await forwarder.read.nonces([claimant.account.address]);
  const request = {
    from: claimant.account.address,
    to: registry.address,
    value: 0n,
    gas: 400_000n,
    deadline: permit.deadline,
    data: encodeFunctionData({
      abi: registry.abi,
      functionName: "submitClaimWithPermit",
      args: [permit, dataPointer, permitSignature],
    }),
  } as const;
  const forwardSignature = await claimant.signTypedData({
    account: claimant.account,
    domain: {
      name: "ClaimsRegistryForwarder",
      version: "1",
      chainId,
      verifyingContract: forwarder.address,
    },
    types: {
      ForwardRequest: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "gas", type: "uint256" },
        { name: "nonce", type: "uint256" },
        { name: "deadline", type: "uint48" },
        { name: "data", type: "bytes" },
      ],
    },
    primaryType: "ForwardRequest",
    message: { ...request, nonce },
  });
  const submissionHash = await forwarder.write.execute(
    [{ ...request, signature: forwardSignature }],
    { account: relayer.account },
  );
  const submissionReceipt = await publicClient.waitForTransactionReceipt({
    hash: submissionHash,
  });

  const claimId = BigInt(iteration);
  const flagged = iteration % 2 === 0;
  const assessmentStatus = flagged ? 4 : 1;
  const fraudScore = flagged ? 6_500 : 3_500;
  const assessmentHash = await registry.write.assessClaim(
    [claimId, assessmentStatus, fraudScore],
    { account: assessor.account },
  );
  const assessmentReceipt = await publicClient.waitForTransactionReceipt({
    hash: assessmentHash,
  });

  if (iteration > 0) {
    observations.push({
      operation: "forwarded_public_submission",
      iteration,
      transaction_hash: submissionHash,
      block_number: submissionReceipt.blockNumber.toString(),
      gas_used: submissionReceipt.gasUsed.toString(),
    });
    observations.push({
      operation: "assessment",
      iteration,
      transaction_hash: assessmentHash,
      block_number: assessmentReceipt.blockNumber.toString(),
      gas_used: assessmentReceipt.gasUsed.toString(),
      status: flagged ? "Flagged" : "UnderReview",
    });
  }
}

const result = {
  schema_version: 1,
  created_at: new Date().toISOString(),
  network: "Hardhat EDR simulated L1",
  chain_id: chainId,
  requested_transactions_per_operation: requested,
  excluded_warmup_transactions_per_operation: 1,
  compiler_profile_required: "production",
  registry_address: registry.address,
  forwarder_address: forwarder.address,
  observations,
};
await mkdir(dirname(output), { recursive: true });
await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
console.log(`Gas benchmark evidence: ${output}`);
