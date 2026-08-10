#!/usr/bin/env bash
#
# Run safe, read-only checks after deployment. No secret values are printed.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
compose_file="${gcp_dir}/compose.yml"
env_file="${1:-${gcp_dir}/.env.gcp}"

if docker info >/dev/null 2>&1; then
  docker_command=(docker)
else
  docker_command=(sudo docker)
fi

compose=(
  "${docker_command[@]}" compose
  --env-file "${env_file}"
  --file "${compose_file}"
)

echo "Container state"
"${compose[@]}" ps

echo
echo "Frontend health"
curl --fail --silent --show-error http://127.0.0.1/healthz

echo
echo "Frontend application"
# The health route can succeed even when Nginx cannot find the compiled React
# files. Checking the page title confirms that the real browser entry point is
# being served, not only that the web server process is alive.
curl --fail --silent --show-error http://127.0.0.1/ \
  | grep -F "<title>Claims Registry | Synthetic Claim Submission</title>"

echo
echo "FastAPI health"
curl --fail --silent --show-error http://127.0.0.1:8000/health
echo

echo
echo "Selected ClaimsRegistry deployment"
"${compose[@]}" exec --no-TTY backend python -c \
  'import os; from packages.integrations.ethereum import load_claims_deployment; d = load_claims_deployment(os.environ); print(f"deployment={d.deployment_id} chain={d.chain_id} address={d.address}")'

echo
echo "Listener monitoring endpoint"
curl --fail --silent --show-error http://127.0.0.1:9101/metrics \
  | grep -E "^claims_listener_(block_lag|last_processed_block|poll_errors_total)"

echo
echo "Scoring monitoring endpoint"
curl --fail --silent --show-error http://127.0.0.1:9102/metrics \
  | grep -E "^claims_scoring_(events_total|processing_seconds_count)"

echo
echo "Scoring quarantine storage"
# This check does not create a test file. It confirms that the non-root worker
# can write its dedicated persistent mount; without that permission the worker
# must fail closed and a poison message would remain uncommitted.
"${compose[@]}" exec --no-TTY scoring-worker sh -c \
  'test -d "$SCORING_STATE_DIR" && test -w "$SCORING_STATE_DIR" && printf "writable: %s\n" "$SCORING_STATE_DIR"'

echo
echo "Kafka topic"
# Read the resolved value from the running listener instead of duplicating a
# topic name here. This verifies the topic the application is actually using.
kafka_topic="$(
  "${compose[@]}" exec --no-TTY listener python -c \
    'import os; print(os.environ["KAFKA_CLAIM_SUBMITTED_TOPIC"])'
)"
"${compose[@]}" exec --no-TTY kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 \
  --describe \
  --topic "${kafka_topic}"

echo
echo "All read-only deployment checks completed."
