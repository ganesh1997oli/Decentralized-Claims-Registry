#!/usr/bin/env bash
#
# Compute Engine runs this script once as root when the research VM is created.
# It installs only the host-level tools; application secrets never enter VM
# metadata, Terraform state or this startup log.

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install --yes --no-install-recommends \
  ca-certificates \
  curl \
  git \
  gnupg

# Install Docker from Docker's signed Ubuntu repository. The Compose plugin is
# included so deployment uses the same `docker compose` syntax as local work.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# VERSION_CODENAME comes from Ubuntu's trusted operating-system metadata.
. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install --yes --no-install-recommends \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
systemctl enable --now docker

# Google's official installer configures the package repository and installs the
# Ops Agent. The repository's deployment script later supplies our small custom
# Docker-log and Prometheus configuration.
ops_installer="/tmp/add-google-cloud-ops-agent-repo.sh"
curl -fsSLo "${ops_installer}" \
  https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
bash "${ops_installer}" --also-install
rm -f "${ops_installer}"

mkdir -p /opt/decentralized-claims-registry
chmod 0755 /opt/decentralized-claims-registry
