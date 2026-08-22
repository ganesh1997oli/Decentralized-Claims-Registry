// Deploy the registry with four separate roles. A permit issuer
// may attest policy eligibility but cannot submit legacy claims, assess claims,
// administer roles, or pay relay gas. The one-day default-admin delay makes an
// accidental transfer recoverable before acceptance.
import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("ClaimsRegistryModule", (m) => {
    const initialAdmin = m.getParameter("initialAdmin");
    const initialSubmitter = m.getParameter("initialSubmitter");
    const initialPermitIssuer = m.getParameter("initialPermitIssuer");
    const initialAssessor = m.getParameter("initialAssessor");
    const adminTransferDelay = m.getParameter(
        "adminTransferDelaySeconds",
        86_400n,
    );
    const claimsForwarder = m.contract("ClaimsForwarder");
    const claimRegistry = m.contract("ClaimsRegistry", [
        initialAdmin,
        initialSubmitter,
        initialPermitIssuer,
        initialAssessor,
        claimsForwarder,
        adminTransferDelay,
    ]);

    return { claimsForwarder, claimRegistry };
});
