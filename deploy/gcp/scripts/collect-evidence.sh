#!/usr/bin/env bash
#
# Capture a small, reviewable evidence bundle for one dissertation experiment.
# The output folder is ignored by Git because logs can contain transaction IDs.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
compose_file="${gcp_dir}/compose.yml"
env_file="${1:-${gcp_dir}/.env.gcp}"
run_name="${2:-$(date -u +%Y%m%dT%H%M%SZ)}"
evidence_dir="${gcp_dir}/evidence/${run_name}"

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

mkdir -p "${evidence_dir}"

# These files contain operational results, not the environment file. Secrets
# therefore remain outside the evidence bundle.
"${compose[@]}" ps > "${evidence_dir}/containers.txt"
"${compose[@]}" logs --no-color --tail 500 listener scoring-worker \
  > "${evidence_dir}/pipeline.log"
curl --fail --silent http://127.0.0.1:9101/metrics \
  > "${evidence_dir}/listener-metrics.prom"
curl --fail --silent http://127.0.0.1:9102/metrics \
  > "${evidence_dir}/scoring-metrics.prom"
curl --fail --silent http://127.0.0.1:9308/metrics \
  > "${evidence_dir}/kafka-metrics.prom"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "${evidence_dir}/collected-at.txt"

echo "Evidence collected in ${evidence_dir}"
echo "Review every file before using it in the dissertation."
