"""Security and release-shape checks for the manually approved GCP pipeline."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "deploy-gcp.yml"
RELEASE_SCRIPT = (
    PROJECT_ROOT / "infrastructure" / "gcp" / "scripts" / "release-vm.sh"
)
CD_TERRAFORM = (
    PROJECT_ROOT / "infrastructure" / "gcp" / "terraform" / "github-cd.tf"
)
MAIN_TERRAFORM = (
    PROJECT_ROOT / "infrastructure" / "gcp" / "terraform" / "main.tf"
)


def _workflow() -> dict[str, object]:
    """Parse GitHub YAML without YAML 1.1 turning the `on` key into true."""

    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def test_deployment_is_manual_main_only_and_serialized() -> None:
    workflow = _workflow()
    triggers = workflow["on"]
    concurrency = workflow["concurrency"]
    assert isinstance(triggers, dict)
    assert isinstance(concurrency, dict)

    assert set(triggers) == {"workflow_dispatch"}
    assert concurrency["cancel-in-progress"] == "false"

    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'GITHUB_REF}" != "refs/heads/main"' in text
    assert "git merge-base --is-ancestor" in text
    assert "Require a successful Continuous integration run" in text


def test_cloud_authority_exists_only_in_approved_deploy_job() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    prepare = jobs["prepare"]
    deploy = jobs["deploy"]
    assert isinstance(prepare, dict)
    assert isinstance(deploy, dict)
    assert isinstance(prepare["permissions"], dict)
    assert isinstance(deploy["permissions"], dict)

    assert "id-token" not in prepare["permissions"]
    assert deploy["permissions"]["id-token"] == "write"
    assert deploy["environment"] == {"name": "gcp-research"}

    # A missing protected environment must fail before cloud authentication.
    variables = deploy["env"]
    assert isinstance(variables, dict)
    assert "GCP_CD_ENABLED" in variables
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in variables
    assert "GCP_DEPLOY_SERVICE_ACCOUNT" in variables


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)

    action_uses: list[str] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            assert isinstance(step, dict)
            if "uses" in step:
                action_uses.append(str(step["uses"]))

    assert action_uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in action_uses)


def test_workflow_contains_no_long_lived_cloud_secret_path() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "credentials_json" not in text
    assert "secrets." not in text
    assert "google-github-actions/auth@" in text
    assert "workload_identity_provider" in text


def test_release_script_is_valid_and_rejects_mutable_refs() -> None:
    syntax = subprocess.run(
        ("bash", "-n", str(RELEASE_SCRIPT)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    invalid = subprocess.run(
        ("bash", str(RELEASE_SCRIPT), "main"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 1
    assert "complete 40-character" in invalid.stderr


def test_release_preserves_private_material_and_has_recovery_controls() -> None:
    text = RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert "flock --exclusive --nonblock" in text
    assert "pre-release.patch" in text
    assert "git_in_repository checkout --detach --force" in text
    assert "git_in_repository merge-base --is-ancestor" in text
    assert "attempting code rollback" in text
    assert "deploy_and_verify" in text
    assert "git_in_repository clean" not in text


def test_release_restores_git_permissions_after_private_backup_umask() -> None:
    text = RELEASE_SCRIPT.read_text(encoding="utf-8")

    # Backups remain private, but Git checkout must not leak that restrictive
    # umask into files copied into an image that runs as UID 10001.
    assert "umask 077" in text
    assert "git_in_repository() (" in text
    assert "umask 022" in text
    assert "git_in_repository ls-files --stage -z" in text
    assert 'chmod 0644 -- "${absolute_path}"' in text
    assert 'chmod 0755 -- "${absolute_path}"' in text

    checkout_count = text.count(
        'git_in_repository checkout --detach --force "${release_sha}"'
    ) + text.count('git_in_repository checkout --detach --force "${current_sha}"')
    assert checkout_count == 2
    assert text.count("normalize_tracked_permissions") == 3


def test_oidc_trust_uses_immutable_ids_and_exact_workflow_context() -> None:
    text = CD_TERRAFORM.read_text(encoding="utf-8")

    assert "assertion.repository_id" in text
    assert "assertion.repository_owner_id" in text
    assert "assertion.workflow_ref" in text
    assert "assertion.event_name == 'workflow_dispatch'" in text
    assert "assertion.environment" in text
    assert "roles/iam.workloadIdentityUser" in text
    assert "roles/compute.viewer" in text
    assert "roles/compute.osAdminLogin" in text
    assert "destination.port == 22" in text
    assert "google_service_account_key" not in text


def test_terraform_manages_every_api_required_for_keyless_ssh() -> None:
    text = MAIN_TERRAFORM.read_text(encoding="utf-8")

    required_services = {
        "cloudresourcemanager.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "oslogin.googleapis.com",
        "sts.googleapis.com",
    }
    assert all(service in text for service in required_services)
