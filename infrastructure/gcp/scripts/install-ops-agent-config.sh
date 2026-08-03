#!/usr/bin/env bash
#
# Install the repository's reviewed Ops Agent configuration on the VM.
# Run this after Compose is up so the three private metrics endpoints exist.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
gcp_dir="$(cd -- "${script_dir}/.." && pwd)"
source_config="${gcp_dir}/monitoring/ops-agent.yaml"
target_config="/etc/google-cloud-ops-agent/config.yaml"

if [[ ! -f "${source_config}" ]]; then
  echo "Ops Agent configuration is missing: ${source_config}" >&2
  exit 1
fi

if ! systemctl list-unit-files google-cloud-ops-agent.service >/dev/null 2>&1; then
  echo "Google Cloud Ops Agent is not installed on this machine." >&2
  exit 1
fi

# `install` writes the complete file with predictable ownership and permissions.
sudo install -o root -g root -m 0644 "${source_config}" "${target_config}"
sudo systemctl restart google-cloud-ops-agent

echo "Ops Agent configuration installed."
echo "Check it with: sudo systemctl status google-cloud-ops-agent --no-pager"
