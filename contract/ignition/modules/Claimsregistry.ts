import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("ClaimsRegistryModule", (m) => {
    const claimRegistry = m.contract("ClaimsRegistry");

    // Convenience for test/demo: authorize the deployer as an assessor so the 
    // submit -> assess flow can be exercised immediately after deployment.
    // In production, authorize specific, known assessor address deliberately
    // (e.g. read them from a module parameter) rather than the deployer.
    const deployer = m.getAccount(0);
    m.call(claimRegistry, "setAssessor", [deployer, true]);

    return { claimRegistry };
});