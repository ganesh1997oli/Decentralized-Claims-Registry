# Preliminary single-host benchmark analysis

## Status and scope

This evidence bundle is an exploratory validation run completed on 25 August
2026. It demonstrates that the benchmark harness operates correctly and gives
an early indication of the system's single-host behavior. It is not yet the
retained dissertation benchmark because the repository was dirty, the sample
sizes do not support stable p99 estimates, and the host was an Apple-silicon
development machine rather than the dissertation's target VM.

The host exposed 14 logical CPUs and 24 GiB of memory. Docker Desktop exposed
14 CPUs and approximately 7.65 GiB of memory. The benchmark used one Uvicorn
process, one PostgreSQL container, one three-partition single-node Kafka broker,
and one synchronous scoring consumer. The model artifact SHA-256 was
`76b020cda978fbe32ec5790261bc6914c1023a32467cdcb90e05aa7fab3e60eb`.

The repository-owned paths remained real: FastAPI validation, canonical claim
authorization and hashing, PostgreSQL transactions, Kafka acknowledgement and
offset commit, duplicate detection, feature persistence, XGBoost, and SHAP.
Deterministic local adapters replaced Pinata/IPFS transport, Ethereum RPC and
block production, and assessment transaction submission. Contract gas was
measured separately on Hardhat's simulated L1.

## HTTP claim-intake results

Each concurrency level contains 300 successful observations across three
repetitions. No HTTP errors occurred among the 1,200 measured claims.

| Concurrent clients | End-to-end p50 (ms) | End-to-end p95 (ms) | Mean throughput (claims/s) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 46.00 | 49.71 | 21.57 | 0 |
| 5 | 74.22 | 89.54 | 65.96 | 0 |
| 10 | 126.41 | 149.75 | 77.44 | 0 |
| 20 | 236.41 | 341.71 | 77.78 | 0 |

Throughput increased sharply between one and ten clients, then remained near
77--78 claims/s. Increasing concurrency from 10 to 20 therefore added queueing
latency without a material throughput gain. This identifies a preliminary
saturation region for the one-process local intake configuration. The timing
includes preparation, client-side EIP-712 signing, and authorization; separate
raw columns retain the preparation, signing, and authorization components.

## Kafka and scoring pipeline results

Each offered-rate group contains 90 successful observations across three
repetitions. No scoring errors occurred among the 360 measured events.

| Offered rate | End-to-end p50 (ms) | End-to-end p95 (ms) | Queue p95 (ms) | Processing p95 (ms) | Achieved throughput (events/s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 5 events/s | 64.66 | 78.41 | 7.25 | 72.99 | 5.11 |
| 10 events/s | 54.56 | 61.91 | 2.97 | 59.82 | 10.14 |
| 20 events/s | 41.15 | 44.08 | 1.70 | 42.51 | 20.07 |
| Unconstrained burst | 585.99 | 1,091.31 | 1,054.16 | 41.30 | 25.99 |

The worker kept pace with controlled input up to 20 events/s and accumulated
little queue residence. Under an unconstrained burst, throughput saturated near
26 events/s while queue p95 rose above one second. The large burst latency was
therefore caused primarily by waiting for the single consumer rather than by
XGBoost/SHAP inference, whose burst p95 was 5.20 ms. Differences in processing
latency between the controlled-rate groups should not be over-interpreted at
this sample size; cache state, host scheduling, and background activity were
not independently controlled.

## Replay recovery

The recovery experiment published 100 events, committed 50, completed the side
effects for one additional event without committing its Kafka offset, and then
discarded the consumer. A consumer in the same group redelivered the crash-window
event. All 100 claims reached a completed state, no claim was lost, and the
assessment registry recorded exactly 100 writes. Recovery of the remaining
unique backlog took 2,328.57 ms after restart. This supports the architectural
claim of at-least-once delivery with idempotent side effects; it does not prove
a system-wide exactly-once guarantee.

## Contract gas

The Hardhat run retained 100 observations per operation after one warm-up
transaction.

| Operation | p50 gas | p95 gas | Minimum | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Forwarded public submission | 271,261 | 271,273 | 270,403 | 272,083 |
| Assessment | 37,728 | 37,747 | 37,709 | 37,747 |

These are gas-unit distributions for locally executed bytecode under the
production compiler profile. They are not transaction latency, Sepolia
throughput, or a fiat-cost estimate.

## Resource observations

The resource collector covered the 90-second HTTP portion only and sampled the
Kafka and PostgreSQL containers, not the host-based Uvicorn and load-generator
processes. Kafka used a median of approximately 444 MiB and a maximum of 559
MiB; PostgreSQL used a median of approximately 70 MiB and a maximum of 86 MiB.
CPU samples exceeded 100% for both containers at peaks, which indicates use of
more than one Docker CPU. These partial observations must not be described as
whole-system resource requirements.

## Interpretation for the dissertation

The earlier statement that no controlled performance evidence was retained can
be replaced only after the full matrix is rerun from a clean, archived commit
on a declared target host. The eventual contribution should still be framed as
provenance, failure recovery, and design validation rather than a claim of
production scalability. A defensible wording is:

> A controlled single-host experiment provides a preliminary baseline for the
> repository-owned processing path. With one API process, intake throughput
> plateaued as concurrency increased, while a single scoring consumer sustained
> controlled rates up to the tested 20 events/s and accumulated queue delay
> under burst load. These measurements exclude public IPFS and Sepolia service
> latency and therefore characterize the local architecture, not production
> capacity or horizontal scalability.

## Required retained run

Before using numeric claims in the final manuscript:

1. Commit the harness and record a clean Git commit.
2. Run the documented HTTP, pipeline, recovery, gas, and resource matrices on
   the declared dissertation VM, preferably the same 2-vCPU/8-GiB shape used by
   the infrastructure configuration.
3. Keep at least 1,000 successful observations per reported group if p99 is to
   be shown; otherwise report p50 and p95 and state that p99 was withheld.
4. Repeat every scenario at least three times, retain errors and warm-up policy,
   and do not discard saturation results.
5. Archive raw CSV/JSON files, manifests, graphs, and `checksums.sha256` in the
   repository release or a permanent archive.
6. Report live Sepolia and Pinata checks separately from the local load test.
