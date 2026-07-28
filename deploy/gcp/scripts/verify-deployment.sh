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
echo "Listener monitoring endpoint"
curl --fail --silent --show-error http://127.0.0.1:9101/metrics \
  | grep -E "^claims_listener_(block_lag|last_processed_block|poll_errors_total)"

echo
echo "Scoring monitoring endpoint"
curl --fail --silent --show-error http://127.0.0.1:9102/metrics \
  | grep -E "^claims_scoring_(events_total|processing_seconds_count)"

echo
echo "Kafka topic"
"${compose[@]}" exec --no-TTY kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 \
  --describe \
  --topic claims.submitted.v1

echo
echo "All read-only deployment checks completed."
