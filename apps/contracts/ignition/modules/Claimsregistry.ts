// Deploy the registry with three intentionally separate roles. The one-day
// default-admin delay makes an accidental transfer recoverable before acceptance.
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
