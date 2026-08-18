// Deploy the registry with five intentionally separate roles. A permit issuer
// may attest policy eligibility but cannot submit legacy claims, assess claims,
// make coverage decisions, administer roles, or pay relay gas. A dedicated
// decision-maker account is the only business role allowed to approve or reject
// a screened claim. The one-day default-admin delay makes an accidental transfer
// recoverable before acceptance.
import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("ClaimsRegistryModule", (m) => {
    const initialAdmin = m.getParameter("initialAdmin");
    const initialSubmitter = m.getParameter("initialSubmitter");
    const initialPermitIssuer = m.getParameter("initialPermitIssuer");
    const initialAssessor = m.getParameter("initialAssessor");
    const initialDecisionMaker = m.getParameter("initialDecisionMaker");
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
        initialDecisionMaker,
        claimsForwarder,
        adminTransferDelay,
    ]);

    return { claimsForwarder, claimRegistry };
});
