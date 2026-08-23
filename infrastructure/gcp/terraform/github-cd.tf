# Keyless GitHub Actions identity for the manually approved research release.
#
# No service-account key is created. GitHub presents a five-minute OIDC token;
# Google validates its immutable repository claims and permits that one workflow
# to impersonate this narrowly scoped deployer service account for the job.

locals {
  github_deploy_workflow_ref = "${var.github_repository}/.github/workflows/deploy-gcp.yml@refs/heads/${var.github_deploy_branch}"
  github_deployer_member = try(
    "serviceAccount:${google_service_account.github_deployer[0].email}",
    null,
  )
}

resource "google_service_account" "github_deployer" {
  count = var.enable_github_cd ? 1 : 0

  project      = var.project_id
  account_id   = "${var.deployment_name}-cd"
  display_name = "Claims research GitHub deployer"
  description  = "Keyless identity used only by the approved main-branch deployment workflow."

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool" "github" {
  count = var.enable_github_cd ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "claims-github"
  display_name              = "Claims GitHub Actions"
  description               = "Trust boundary for the claims registry deployment workflow."

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.enable_github_cd ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "main-deploy"
  display_name                       = "Reviewed main deployment"
  description                        = "Accepts only the exact manual deployment workflow on the reviewed main branch."

  # Map every claim referenced by the admission condition. Numeric repository
  # and owner IDs survive renames and cannot be reclaimed by another account.
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.environment"         = "assertion.environment"
    "attribute.event_name"          = "assertion.event_name"
    "attribute.ref"                 = "assertion.ref"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
  }

  # Defense in depth: even another workflow in this repository cannot mint the
  # deployer identity, and a feature branch cannot deploy before it is reviewed.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.ref == 'refs/heads/${var.github_deploy_branch}'",
    "assertion.workflow_ref == '${local.github_deploy_workflow_ref}'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.environment == '${var.github_deploy_environment}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com/"
  }
}

# Only identities admitted by the provider and carrying this exact immutable
# repository ID may impersonate the deployment service account.
resource "google_service_account_iam_member" "github_workload_identity_user" {
  count = var.enable_github_cd ? 1 : 0

  service_account_id = google_service_account.github_deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository_id/${var.github_repository_id}"
}

# The deployer can discover the one VM but cannot create, delete, resize, stop,
# or start Compute Engine resources.
resource "google_project_iam_member" "github_compute_viewer" {
  count = var.enable_github_cd ? 1 : 0

  project = var.project_id
  role    = "roles/compute.viewer"
  member  = local.github_deployer_member
}

# OS Admin Login provides a short-lived Linux identity and sudo through OS
# Login. No persistent SSH private key is stored in GitHub.
resource "google_project_iam_member" "github_os_admin_login" {
  count = var.enable_github_cd ? 1 : 0

  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = local.github_deployer_member
}

# IAP is the only network path to SSH. The condition prevents this role from
# being reused for another TCP destination port in the project.
resource "google_project_iam_member" "github_iap_ssh" {
  count = var.enable_github_cd ? 1 : 0

  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = local.github_deployer_member

  condition {
    title       = "github-cd-ssh-port-only"
    description = "Permit the approved workflow to open IAP tunnels only to SSH."
    expression  = "destination.port == 22"
  }
}

# Compute Engine checks actAs on the VM's attached identity during OS Login.
# That identity itself can write only logs and metrics, so this does not grant
# the deployer application secrets or broader project administration.
resource "google_service_account_iam_member" "github_vm_service_account_user" {
  count = var.enable_github_cd ? 1 : 0

  service_account_id = google_service_account.vm.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.github_deployer_member
}
