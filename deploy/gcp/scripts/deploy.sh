#!/usr/bin/env bash
#
# Build and start the complete single-VM research pipeline. The script validates
# the configuration before Docker creates or changes any containers.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${gcp_dir}/../.." && pwd)"
compose_file="${gcp_dir}/compose.yml"
env_file="${1:-${gcp_dir}/.env.gcp}"

if [[ ! -f "${env_file}" ]]; then
  echo "Deployment environment file is missing: ${env_file}" >&2
  echo "Copy .env.gcp.example to .env.gcp and replace every CHANGE_ME value." >&2
  exit 1
fi

# Comments can explain the placeholder text; only unresolved variable values
# should block a deployment.
if grep -Eq '^[A-Za-z_][A-Za-z0-9_]*=.*CHANGE_ME' "${env_file}"; then
  echo "The deployment environment still contains CHANGE_ME placeholders." >&2
  exit 1
fi

# Read only the model path instead of sourcing the environment file as shell
# code. This avoids executing an accidental command from a configuration file.
model_host_dir="$(
  sed -n 's/^XGBOOST_MODEL_HOST_DIR=//p' "${env_file}" | tail -n 1
)"
model_host_dir="${model_host_dir%\"}"
model_host_dir="${model_host_dir#\"}"
model_host_dir="${model_host_dir%\'}"
model_host_dir="${model_host_dir#\'}"
if [[ "${model_host_dir}" != /* ]]; then
  model_host_dir="${gcp_dir}/${model_host_dir}"
fi

if [[ ! -f "${model_host_dir}/model.joblib" ]] \
  || [[ ! -f "${model_host_dir}/metadata.json" ]]; then
  echo "The reviewed XGBoost artifact is missing from ${model_host_dir}." >&2
  echo "Create it first with: ${gcp_dir}/scripts/train-model.sh ${env_file}" >&2
  exit 1
fi

# Most VM users join the docker group after first login. Falling back to sudo
# keeps the script usable immediately without weakening Docker's socket.
if docker info >/dev/null 2>&1; then
  docker_command=(docker)
elif sudo docker info >/dev/null 2>&1; then
  docker_command=(sudo docker)
else
  echo "Docker is not running or the current user cannot access it." >&2
  exit 1
fi

compose=(
  "${docker_command[@]}" compose
  --env-file "${env_file}"
  --file "${compose_file}"
)

cd "${repo_root}"

echo "Validating the resolved Compose configuration..."
"${compose[@]}" config --quiet

echo "Building the application and frontend images..."
"${compose[@]}" build

echo "Starting Kafka, PostgreSQL and the application processes..."
"${compose[@]}" up --detach --remove-orphans

echo
echo "Deployment started. Verify it with:"
echo "  ${gcp_dir}/scripts/verify-deployment.sh ${env_file}"
