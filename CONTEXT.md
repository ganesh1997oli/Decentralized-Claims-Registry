# Claims Registry

This context describes the people, organizations, and records involved when an
individual lodges an insurance claim and an insurer reviews it.

## Language

**Claimant**:
The person whose loss is being claimed. The claimant can authorize a representative to submit on their behalf.
_Avoid_: Submitter, insurer wallet

**Policyholder**:
The person or organization that owns the insurance policy. A policyholder and claimant may be different people.
_Avoid_: Account, user

**Submitter**:
The person who signs and lodges a claim. The submitter is usually the claimant, but may be an authorized representative.
_Avoid_: Claimant when representation is possible, relayer

**Insurer**:
The regulated organization that issued the policy and owns the coverage decision.
_Avoid_: Submitter, assessor

**Policy Eligibility**:
An insurer-backed determination that a policy existed, covered the incident date, and allowed the submitter to lodge a claim for the claimant.
_Avoid_: Claim approval, fraud check

**Claim Permit**:
A short-lived, single-use insurer attestation allowing one submitter to anchor one exact eligible claim.
_Avoid_: API key, permanent submitter role

**Fraud Screening**:
A risk signal used to prioritize review. It is neither proof of fraud nor a coverage decision.
_Avoid_: Claim decision, rejection

**Coverage Decision**:
The insurer's determination to approve or reject the claim under the policy.
_Avoid_: Fraud score, model outcome

**Relayer**:
The unprivileged technical account that pays transaction fees for an already authorized submission.
_Avoid_: Submitter, claimant, insurer
