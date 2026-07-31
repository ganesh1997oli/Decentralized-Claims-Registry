#!/usr/bin/env bash
#
# Stop containers to conserve free-trial credit while keeping Kafka, PostgreSQL
# and the listener checkpoint volumes available for the next demonstration.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
env_file="${1:-${gcp_dir}/.env.gcp}"

if docker info >/dev/null 2>&1; then
  docker_command=(docker)
else
  docker_command=(sudo docker)
fi

"${docker_command[@]}" compose \
  --env-file "${env_file}" \
  --file "${gcp_dir}/compose.yml" \
  stop

echo "Containers stopped. Named data volumes were not deleted."
