#!/usr/bin/env bash
#
# Install one reviewed main-branch commit on the single research VM.
#
# GitHub Actions copies this script from the selected commit to /tmp, then runs
# it with sudo over IAP. The private .env.gcp, mounted testnet keys, Terraform
# state, Docker volumes, and model artifact never leave the VM. This script does
# not run `git clean`, so ignored deployment material survives every release.

set -Eeuo pipefail

# Backups and state records are root-readable only. They can contain a patch of
# an earlier manual hotfix and therefore must not become public build artifacts.
umask 077

readonly repository_root="/opt/decentralized-claims-registry"
readonly environment_file="${repository_root}/infrastructure/gcp/.env.gcp"
readonly secret_directory="${repository_root}/infrastructure/gcp/.env.gcp-secrets"
readonly deployment_script="${repository_root}/infrastructure/gcp/scripts/deploy.sh"
readonly verification_script="${repository_root}/infrastructure/gcp/scripts/verify-deployment.sh"
readonly state_directory="/var/lib/claims-registry-deployments"
readonly backup_directory="${state_directory}/backups"
readonly lock_file="${state_directory}/deployment.lock"
readonly release_sha="${1:-}"

fail() {
  printf 'Release refused: %s\n' "$1" >&2
  exit 1
}

# Validate the only caller-controlled argument before doing privileged work.
# A complete SHA is immutable and contains no shell metacharacters.
if [[ ! "${release_sha}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "expected one complete 40-character lowercase Git commit SHA"
fi

if [[ "${EUID}" -ne 0 ]]; then
  fail "run this script through sudo on the deployment VM"
fi

for command_name in chmod dirname flock git runuser stat; do
  command -v "${command_name}" >/dev/null 2>&1 \
    || fail "required host command is unavailable: ${command_name}"
done

[[ -d "${repository_root}/.git" ]] \
  || fail "the deployment repository is missing at ${repository_root}"
[[ -f "${environment_file}" ]] \
  || fail "the ignored deployment environment is missing"
[[ -d "${secret_directory}" ]] \
  || fail "the ignored deployment key directory is missing"

install -d -m 0700 "${state_directory}" "${backup_directory}"
exec 9>"${lock_file}"
if ! flock --exclusive --nonblock 9; then
  fail "another deployment is already running"
fi

# The repository belongs to the human OS Login account used during setup. Run
# Git as that owner so releases do not turn the checkout into root-owned files.
repository_owner="$(stat -c '%U' "${repository_root}/.git")"
[[ -n "${repository_owner}" ]] || fail "could not determine repository owner"

git_in_repository() (
  # The root process keeps umask 077 for private patches and state. Git-created
  # source must instead remain readable by the non-root UID used in the images.
  umask 022

  if [[ "${repository_owner}" == "root" ]]; then
    git -c safe.directory="${repository_root}" -C "${repository_root}" "$@"
  else
    runuser -u "${repository_owner}" -- \
      git -C "${repository_root}" "$@"
  fi
)

normalize_tracked_permissions() {
  local absolute_path
  local directory_path
  local index_entry
  local index_metadata
  local index_mode
  local tracked_path

  # A previous release may already have produced 0600 files or 0700 source
  # directories. Restore every indexed regular file to its Git executable bit
  # and make only its tracked parent directories traversable. Ignored secrets,
  # Terraform state, model artifacts, and .git are never enumerated or changed.
  while IFS= read -r -d '' index_entry; do
    index_metadata="${index_entry%%$'\t'*}"
    tracked_path="${index_entry#*$'\t'}"
    index_mode="${index_metadata%% *}"
    absolute_path="${repository_root}/${tracked_path}"

    directory_path="$(dirname -- "${absolute_path}")"
    while [[ "${directory_path}" != "${repository_root}" ]]; do
      chmod 0755 -- "${directory_path}"
      directory_path="$(dirname -- "${directory_path}")"
    done

    case "${index_mode}" in
      100644)
        chmod 0644 -- "${absolute_path}"
        ;;
      100755)
        chmod 0755 -- "${absolute_path}"
        ;;
      120000)
        # chmod would follow a symlink, so leave Git-managed links untouched.
        ;;
      *)
        fail "unsupported tracked Git mode ${index_mode} for ${tracked_path}"
        ;;
    esac
  done < <(git_in_repository ls-files --stage -z)
}

current_sha="$(git_in_repository rev-parse HEAD)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

# The first CD release may encounter files previously copied with scp. Preserve
# a private patch and status listing before the reviewed commit replaces them.
# This is a recovery aid, not a source of truth; GitHub remains the release log.
if [[ -n "$(git_in_repository status --short --untracked-files=no)" ]]; then
  patch_file="${backup_directory}/${timestamp}-${current_sha}-pre-release.patch"
  status_file="${backup_directory}/${timestamp}-${current_sha}-pre-release.status"
  git_in_repository diff --binary HEAD >"${patch_file}"
  git_in_repository status --short --untracked-files=no >"${status_file}"
  printf 'Saved pre-release tracked changes to %s\n' "${patch_file}"
fi

origin_url="$(git_in_repository remote get-url origin)"
case "${origin_url}" in
  https://github.com/ganesh1997oli/Decentralized-Claims-Registry | \
    https://github.com/ganesh1997oli/Decentralized-Claims-Registry.git)
    ;;
  *)
    fail "origin does not identify the reviewed claims registry repository"
    ;;
esac

printf 'Fetching reviewed main history...\n'
git_in_repository fetch --no-tags origin \
  "+refs/heads/main:refs/remotes/origin/main"

git_in_repository cat-file -e "${release_sha}^{commit}" \
  || fail "requested release commit is unavailable after fetching main"
git_in_repository merge-base --is-ancestor \
  "${release_sha}" refs/remotes/origin/main \
  || fail "requested release commit is not part of origin/main history"

deploy_and_verify() {
  # deploy.sh validates configuration before changing containers. Its Compose
  # project name is fixed in .env.gcp, so named database/Kafka volumes remain
  # attached even though the Git commit changes.
  bash "${deployment_script}" "${environment_file}" \
    && bash "${verification_script}" "${environment_file}"
}

record_success() {
  local deployed_sha="$1"
  printf '%s\n' "${deployed_sha}" >"${state_directory}/last-successful-sha"
  {
    printf 'deployed_sha=%s\n' "${deployed_sha}"
    printf 'previous_sha=%s\n' "${current_sha}"
    printf 'deployed_at=%s\n' "${timestamp}"
  } >"${state_directory}/last-successful-release"
}

printf 'Checking out immutable release %s...\n' "${release_sha}"
# --force is intentional and bounded to tracked repository files. Any dirty
# tracked state was backed up above; ignored secrets and model files are kept.
git_in_repository checkout --detach --force "${release_sha}"
normalize_tracked_permissions

if deploy_and_verify; then
  record_success "${release_sha}"
  printf 'Release %s passed all deployment checks.\n' "${release_sha}"
  exit 0
else
  release_status=$?
fi

printf 'Release %s failed; attempting code rollback to %s...\n' \
  "${release_sha}" "${current_sha}" >&2

# A rollback returns source and images to the former commit. Database migrations
# are forward-only; every migration merged to main must therefore remain
# backward-compatible with the previous release.
if [[ "${current_sha}" != "${release_sha}" ]]; then
  git_in_repository checkout --detach --force "${current_sha}"
  normalize_tracked_permissions
  if deploy_and_verify; then
    printf 'Rollback to %s succeeded. The requested release remains failed.\n' \
      "${current_sha}" >&2
  else
    printf 'Rollback to %s also failed; inspect VM and Compose logs immediately.\n' \
      "${current_sha}" >&2
  fi
fi

exit "${release_status}"
