#!/usr/bin/env bash
#
# Reproduce the reviewed XGBoost artifact inside the same image that will serve
# predictions. The output directory is mounted from the VM and is later mounted
# read-only into the scoring worker.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
env_file="${1:-${gcp_dir}/.env.gcp}"

if [[ ! -f "${env_file}" ]]; then
  echo "Deployment environment file is missing: ${env_file}" >&2
  exit 1
fi

if docker info >/dev/null 2>&1; then
  docker_command=(docker)
else
  docker_command=(sudo docker)
fi

compose=(
  "${docker_command[@]}" compose
  --env-file "${env_file}"
  --file "${gcp_dir}/compose.yml"
  --profile training
)

echo "Building the shared Python runtime..."
"${compose[@]}" build model-trainer

echo "Training the pinned research model and writing its checksum metadata..."
"${compose[@]}" run --rm model-trainer

echo
echo "Model training completed. Review packages/model/RESULTS.md and the generated metadata"
echo "before treating this artifact as approved for a dissertation experiment."
