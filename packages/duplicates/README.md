# Cross-insurer duplicate screening

This package checks whether the same incident has already been submitted by a
different insurer. It creates a review signal, not a fraud decision.

## Quick mental model

This is closer to a **privacy-conscious exact-match alarm** than a similarity
search. Two normalized synthetic incidents produce the same keyed fingerprint;
the database can compare those fingerprints without storing another copy of
the description or policy reference.

| Boundary | Duplicate detector responsibility |
| --- | --- |
| Receives | Validated incident fields, insurer ID, deployment identity and a private HMAC key |
| Produces | Versioned incident fingerprint and the IDs/insurers of exact cross-insurer matches |
| Changes | Feature snapshot and human-review presentation only |
| Does not change | Model probability, contract status, claim approval or human outcome |

The word “duplicate” means “same selected fields after normalization.” It does
not mean the two claims are fraudulent, identical in every detail, or submitted
by the same person.

## Architecture

```mermaid
flowchart LR
    Claim["Verified claim from IPFS"] --> Normalize["Normalize incident fields"]
    Normalize --> Fingerprint["Create keyed HMAC-SHA256 fingerprint"]
    Fingerprint --> Database[("PostgreSQL")]
    Database --> Match{"Same fingerprint from another insurer?"}
    Match -->|Yes| Review["Flag for review"]
    Match -->|No| Continue["Continue normally"]
    Review --> Snapshot["Save feature snapshot"]
    Continue --> Snapshot
```

`detector.py` normalizes selected incident fields and creates the fingerprint.
`PostgresDuplicateRepository` stores it and searches for the same fingerprint
from another insurer on the same chain and registry contract. The scoring
worker runs this check before saving the claim's feature snapshot.

The fingerprint uses the incident date, amount, claim type, vehicle details,
region, country, and injury/total-loss flags. It excludes insurer, claim and
policy references so that different insurers can produce the same fingerprint.

## Data boundary

The duplicate-matching table stores the fingerprint rather than the canonical
payload. The scoring worker reads the private `DUPLICATE_FINGERPRINT_KEY`, which
must contain at least 32 bytes. A keyed HMAC is used instead of a plain hash to
make offline guessing of predictable claim values more difficult.

In this prototype, cross-insurer matching means different insurer IDs using the
same private service and PostgreSQL database. It does not query independent
databases owned by other insurers.

## Limitations

- PostgreSQL is a central trust and availability dependency.
- Matching is exact after normalization; a small change to a date, amount or
  category creates a different fingerprint.
- Similar legitimate incidents may be flagged.
- Results only include claims already processed by this deployment.
- Key rotation and access governance are not fully implemented.
- Every match requires human review and must not be treated as proof of fraud.

## Relevant code

| File | Responsibility |
| --- | --- |
| `detector.py` | Normalization, fingerprint generation and result types |
| `../integrations/postgres/duplicate_repository.py` | Atomic storage and cross-insurer lookup |
| `../integrations/postgres/feature_processor.py` | Adds the match count to the feature snapshot |
| `../integrations/kafka/scoring_worker.py` | Runs the check in the claim workflow |
