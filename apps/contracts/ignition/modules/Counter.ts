// Hardhat scaffold retained as a minimal Ignition example; it is not part of the
// claims workflow or selected by CLAIMS_DEPLOYMENT_ID.
import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

export default buildModule("CounterModule", (m) => {
  const counter = m.contract("Counter");

  m.call(counter, "incBy", [5n]);

  return { counter };
});
