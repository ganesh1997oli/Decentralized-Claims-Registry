# Local single-host performance baseline

This directory measures the repository-owned processing path without applying
sustained load to Sepolia or Pinata. It is a preliminary research baseline, not
evidence of production scalability or high availability.

The experiments keep FastAPI routing and validation, canonical claim hashing,
PostgreSQL, Kafka, duplicate detection, feature processing, XGBoost and local
SHAP real. Deterministic adapters replace IPFS transport, Ethereum RPC/block
production, and the assessment transaction. A separate Hardhat script records
contract gas usage under the production compiler profile.

## Safety boundary

- The benchmark FastAPI process binds to `127.0.0.1` only.
- Its deterministic wallet derivation and rate exemption are intentionally
  benchmark-only and are unavailable from the production application entry
  point.
- The Compose project uses dedicated ports, database, Kafka storage, topic
  prefixes and consumer-group prefixes.
- Every workload scenario creates a uniquely named PostgreSQL schema and Kafka
  topic. These are removed after integrity checks unless `--keep-schema` is
  selected.
- Do not place `.env.local`, wallet keys, Pinata tokens, database dumps, or HMAC
  secrets in a results directory.

## 1. Verify the runtime

From the repository root:

```bash
docker --version
docker compose version
apps/backend/.venv/bin/python --version
apps/backend/.venv/bin/python -m pytest --version
```

The Python environment must contain the locked production and research
dependencies, including psycopg, confluent-kafka, XGBoost, SHAP, aiohttp, and
matplotlib.

## 2. Start isolated infrastructure

```bash
docker compose -f benchmarks/local/compose.yml up -d postgres kafka
docker compose -f benchmarks/local/compose.yml ps
```

Expected host endpoints:

| Dependency | Endpoint |
| --- | --- |
| PostgreSQL | `127.0.0.1:55432` |
| Kafka | `127.0.0.1:19092` |
| Optional Kafka exporter | `127.0.0.1:19308` |

To include the optional exporter:

```bash
docker compose -f benchmarks/local/compose.yml \
  --profile monitoring up -d
```

## 3. Run smoke tests

Use small workloads first. Each command creates a timestamped evidence folder
under `benchmarks/local/results/`.

HTTP preparation plus EIP-712 signing and authorization:

```bash
apps/backend/.venv/bin/python -m benchmarks.local.http_load \
  --concurrency 1 \
  --requests-per-level 10 \
  --warmup-requests 1 \
  --repetitions 1
```

Kafka-to-assessment pipeline with the real reviewed model and SHAP:

```bash
MPLCONFIGDIR=/tmp/claims-benchmark-matplotlib \
  apps/backend/.venv/bin/python -m benchmarks.local.pipeline_load \
  --rates 0 \
  --events-per-rate 5 \
  --warmup-events 1 \
  --repetitions 1
```

Replay recovery after a handler side effect but before its Kafka offset commit:

```bash
MPLCONFIGDIR=/tmp/claims-benchmark-matplotlib \
  apps/backend/.venv/bin/python -m benchmarks.local.recovery_test \
  --backlog 6 \
  --crash-after 3
```

The recovery run fails unless the uncommitted event is redelivered, every claim
completes, and the deterministic assessment registry receives no duplicate
write.

## 4. Run the retained HTTP matrix

The following produces at least 1,000 observations per concurrency level, so
p50 and p95 are well supported. The analysis script emits p99 only when a group
contains at least 1,000 successful values.

```bash
apps/backend/.venv/bin/python -m benchmarks.local.http_load \
  --concurrency 1,5,10,20,40 \
  --requests-per-level 1000 \
  --warmup-requests 50 \
  --repetitions 3
```

One Uvicorn worker is used because the dissertation deployment runs one API
process. Every scenario receives a fresh schema and server process, so table
growth from a previous concurrency level does not bias the next level.

HTTP `end_to_end_ms` covers preparation, local EIP-712 signing and authorization.
The separate `prepare_ms`, `sign_ms`, and `authorize_ms` columns prevent client
cryptography from being mistaken for server latency.

## 5. Run the retained asynchronous matrix

`0` means that the publisher supplies a burst as quickly as Kafka acknowledges
each event. Positive values are closed, controlled offered rates in events per
second.

```bash
MPLCONFIGDIR=/tmp/claims-benchmark-matplotlib \
  apps/backend/.venv/bin/python -m benchmarks.local.pipeline_load \
  --rates 0,2,5,10,20,30 \
  --events-per-rate 400 \
  --warmup-events 10 \
  --repetitions 3 \
  --timeout 1800
```

The pipeline output retains Kafka acknowledgement, queue residence, XGBoost plus
SHAP inference, handler processing, and enqueue-to-completion latency. Throughput
counts only events that reached a completed PostgreSQL assessment.

If a rate exceeds the worker's capacity, queue delay will rise and achieved
throughput will fall below offered throughput. Retain that result; it identifies
the approximate saturation region rather than a failed experiment.

## 6. Run the replay-recovery experiment

```bash
MPLCONFIGDIR=/tmp/claims-benchmark-matplotlib \
  apps/backend/.venv/bin/python -m benchmarks.local.recovery_test \
  --backlog 500 \
  --crash-after 250 \
  --timeout 3600
```

This experiment processes and commits 250 messages, processes one additional
message without committing its offset, discards that consumer, then starts a new
consumer in the same group. The first post-restart delivery must be a safe replay
of the already completed event.

## 7. Collect resources during a long run

Start the collector in another terminal. It records benchmark containers and
host load once per second until interrupted:

```bash
apps/backend/.venv/bin/python -m benchmarks.local.collect_resources \
  --output benchmarks/local/results/resource-usage.csv \
  --interval 1
```

Pass `--pid <PID>` to include the host Uvicorn or workload process. The collector
retains raw CPU/memory values; it does not calculate or invent missing values.

## 8. Record contract gas

From `apps/contracts/`, run the checked-in gas script with the same optimized
compiler profile used for the deployment build:

```bash
BENCHMARK_GAS_TRANSACTIONS=1000 \
BENCHMARK_GAS_OUTPUT=../../benchmarks/local/results/gas-results.json \
  npm exec -- hardhat run scripts/benchmark-gas.ts \
  --build-profile production
```

The JSON contains `gasUsed` for the complete forwarded public-submission call
and the assessor write. The retained matrix uses 1,000 observations per
operation so the analysis may report p99 without implying that a smaller tail
sample is stable. It deliberately reports gas units, not a time-dependent fiat
estimate.

## 9. Analyse retained outputs

Analyse one run directory or the whole results root:

```bash
apps/backend/.venv/bin/python -m benchmarks.local.analyse_results \
  --input benchmarks/local/results
```

Generated evidence includes:

- `summary.json`;
- `latency-vs-concurrency.png`;
- `throughput-vs-concurrency.png`;
- `pipeline-stage-latency.png`;
- `kafka-lag-recovery.png`;
- `gas-distribution.png` when gas observations are present;
- `resource-usage.png` when resource samples are present; and
- `checksums.sha256` for the retained evidence bundle.

Raw CSV/JSON files remain the authoritative evidence. Graphs are derived views.

## 10. Stop services

This retains dedicated benchmark volumes:

```bash
docker compose -f benchmarks/local/compose.yml down
```

Remove volumes only when the retained result bundle has been reviewed and the
benchmark database is deliberately no longer needed:

```bash
docker compose -f benchmarks/local/compose.yml down --volumes
```

## Interpretation boundary

These measurements characterize one process layout, one single-node Kafka
broker, one PostgreSQL instance, synthetic inputs, and deterministic substitutes
for public-network dependencies. They do not establish horizontal scalability,
multi-zone availability, production insurance capacity, Sepolia throughput, or
Pinata service quality. A small funded live-Sepolia verification should be
reported separately as operational evidence rather than combined with this load
test.
