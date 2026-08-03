import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("ClaimsRegistryModule", (m) => {
    const initialAdmin = m.getParameter("initialAdmin");
    const initialSubmitter = m.getParameter("initialSubmitter");
    const initialAssessor = m.getParameter("initialAssessor");
    const adminTransferDelay = m.getParameter(
        "adminTransferDelaySeconds",
        86_400n,
    );
    const claimRegistry = m.contract("ClaimsRegistry", [
        initialAdmin,
        initialSubmitter,
        initialAssessor,
        adminTransferDelay,
    ]);

    return { claimRegistry };
});
