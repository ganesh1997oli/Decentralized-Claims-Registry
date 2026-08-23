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

# Return one unquoted value without sourcing the environment file as shell code.
# Treating configuration as data prevents an accidental shell command in the
# file from running during validation.
read_env_value() {
  local variable_name="$1"
  local value
  value="$(
    sed -n "s/^${variable_name}=//p" "${env_file}" | tail -n 1
  )"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "${value}"
}

deployment_id="$(read_env_value CLAIMS_DEPLOYMENT_ID)"
kafka_topic="$(read_env_value KAFKA_CLAIM_SUBMITTED_TOPIC)"
kafka_consumer_group="$(read_env_value KAFKA_CONSUMER_GROUP_ID)"
expected_kafka_topic="claims.submitted.${deployment_id}"
expected_kafka_consumer_group="claims-registry-scorer-${deployment_id}"

# A topic contains claim events, while a consumer group stores progress through
# those events. Requiring both names to include the deployment ID prevents a new
# contract deployment from sharing either data stream or offsets with an old one.
if [[ "${kafka_topic}" != "${expected_kafka_topic}" ]]; then
  echo "KAFKA_CLAIM_SUBMITTED_TOPIC must be scoped to CLAIMS_DEPLOYMENT_ID." >&2
  echo "Expected: ${expected_kafka_topic}" >&2
  exit 1
fi

if [[ "${kafka_consumer_group}" != "${expected_kafka_consumer_group}" ]]; then
  echo "KAFKA_CONSUMER_GROUP_ID must be scoped to CLAIMS_DEPLOYMENT_ID." >&2
  echo "Expected: ${expected_kafka_consumer_group}" >&2
  exit 1
fi

model_host_dir="$(read_env_value XGBOOST_MODEL_HOST_DIR)"
if [[ "${model_host_dir}" != /* ]]; then
  model_host_dir="${gcp_dir}/${model_host_dir}"
fi

if [[ ! -f "${model_host_dir}/model.joblib" ]] \
  || [[ ! -f "${model_host_dir}/metadata.json" ]]; then
  echo "The reviewed XGBoost artifact is missing from ${model_host_dir}." >&2
  echo "Create it first with: ${gcp_dir}/scripts/train-model.sh ${env_file}" >&2
  exit 1
fi

required_values=(
  PUBLIC_HOST
  FRONTEND_ORIGINS
  CLAIMANT_AUTH_DOMAIN
  CLAIMANT_AUTH_URI
  CLAIMANT_SESSION_SIGNING_KEY
  CLAIMANT_SUBJECT_KEY
  CLAIMANT_AUTH_FINGERPRINT_KEY
  POLICY_REFERENCE_LOOKUP_KEY
  CLAIMANT_COMMITMENT_KEY
  POLICY_ELIGIBILITY_RECORDS_JSON
  CLAIM_PERMIT_ISSUER_KEYS_HOST_DIR
  CLAIM_PERMIT_ISSUERS_JSON
  CLAIM_AUTHORIZATION_KEY
  GASLESS_REQUEST_FINGERPRINT_KEY
  PUBLIC_DEMO_READ_ONLY
  INDEXER_OPERATIONS_API_KEY_SHA256
  ASSESSOR_OUTCOME_CREDENTIALS_JSON
  POSTGRES_PASSWORD
  SEPOLIA_RELAYER_PRIVATE_KEY_HOST_FILE
  SEPOLIA_ASSESSOR_PRIVATE_KEY_HOST_FILE
  PINATA_JWT
  DUPLICATE_FINGERPRINT_KEY
  LISTENER_START_BLOCK
)
for variable_name in "${required_values[@]}"; do
  if [[ -z "$(read_env_value "${variable_name}")" ]]; then
    echo "${variable_name} must be set in the deployment environment." >&2
    exit 1
  fi
done

public_host="$(read_env_value PUBLIC_HOST)"
frontend_origins="$(read_env_value FRONTEND_ORIGINS)"
claimant_auth_domain="$(read_env_value CLAIMANT_AUTH_DOMAIN)"
claimant_auth_uri="$(read_env_value CLAIMANT_AUTH_URI)"
if [[ "${public_host}" == *"://"* ]] \
  || [[ "${public_host}" == */* ]] \
  || [[ "${public_host}" =~ [[:space:]] ]]; then
  echo "PUBLIC_HOST must be one hostname without a scheme, path or whitespace." >&2
  exit 1
fi
if [[ "${frontend_origins}" != "https://${public_host}" ]] \
  || [[ "${claimant_auth_domain}" != "${public_host}" ]] \
  || [[ "${claimant_auth_uri}" != "https://${public_host}" ]]; then
  echo "PUBLIC_HOST, FRONTEND_ORIGINS and claimant-auth settings must select the same HTTPS origin." >&2
  exit 1
fi

if [[ "$(read_env_value ALLOW_RATE_LIMIT_BYPASS)" != "false" ]]; then
  echo "ALLOW_RATE_LIMIT_BYPASS must be false for a public deployment." >&2
  exit 1
fi

public_demo_read_only="$(read_env_value PUBLIC_DEMO_READ_ONLY)"
if [[ "${public_demo_read_only}" != "true" ]] \
  && [[ "${public_demo_read_only}" != "false" ]]; then
  echo "PUBLIC_DEMO_READ_ONLY must be exactly true or false." >&2
  exit 1
fi

resolve_host_path() {
  local configured_path="$1"
  if [[ "${configured_path}" == /* ]]; then
    printf '%s' "${configured_path}"
  else
    printf '%s/%s' "${gcp_dir}" "${configured_path}"
  fi
}

permit_key_dir="$(resolve_host_path "$(read_env_value CLAIM_PERMIT_ISSUER_KEYS_HOST_DIR)")"
relayer_key_file="$(resolve_host_path "$(read_env_value SEPOLIA_RELAYER_PRIVATE_KEY_HOST_FILE)")"
assessor_key_file="$(resolve_host_path "$(read_env_value SEPOLIA_ASSESSOR_PRIVATE_KEY_HOST_FILE)")"
if [[ ! -d "${permit_key_dir}" ]]; then
  echo "The permit-issuer key directory is missing." >&2
  exit 1
fi
if [[ ! -f "${relayer_key_file}" ]]; then
  echo "The relayer key file is missing." >&2
  exit 1
fi
if [[ ! -f "${assessor_key_file}" ]]; then
  echo "The assessor key file is missing." >&2
  exit 1
fi

if [[ -n "$(read_env_value SEPOLIA_RELAYER_PRIVATE_KEY)" ]] \
  || [[ -n "$(read_env_value SEPOLIA_ASSESSOR_PRIVATE_KEY)" ]]; then
  echo "Public deployments must mount signer keys from files, not environment values." >&2
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
