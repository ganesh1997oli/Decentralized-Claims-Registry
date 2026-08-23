# Manually approved GitHub CD to the research VM

This runbook adds a production-style release process to the existing single-VM
research deployment. It improves repeatability and auditability; it does not
turn one VM, one PostgreSQL container, and one Kafka broker into a highly
available production insurance platform.

The workflow is deliberately **continuous delivery with a human release gate**:

```text
feature branch -> pull request -> main -> CI passes
                                      -> maintainer clicks Deploy
                                      -> environment approval
                                      -> keyless GCP login
                                      -> immutable SHA on VM
                                      -> deploy + verify
                                      -> code rollback if verification fails
```

Nothing deploys on `push` or `pull_request`. The `workflow_dispatch` trigger is
available in the GitHub interface only after `deploy-gcp.yml` exists on the
default branch. Developing it on `production-cd-pipeline` is safe; merge it to
`main` only after reviewing the pull request and successful CI.

## What each boundary owns

| Boundary | Owns | Explicitly does not own |
| --- | --- | --- |
| GitHub repository | Reviewed source, workflow and commit history | `.env.gcp`, signing keys, raw API keys, Terraform state |
| GitHub `gcp-research` Environment | Human approval rules and non-secret GCP resource names | Application credentials |
| Workload Identity Federation | Short-lived trust from one exact workflow context | A downloadable service-account key |
| GCP deployer service account | VM discovery, IAP SSH and OS Admin Login | VM lifecycle, billing, network administration, Secret Manager, application keys |
| VM | Private environment, mounted testnet keys, model artifact and Docker volumes | GitHub credentials |
| `release-vm.sh` | Commit validation, release lock, backup, deploy, verification and code rollback | Terraform apply, VM start/stop, database downgrade |

The Google trust condition checks all of these claims before issuing a token:

- immutable repository ID `1265286953`;
- immutable owner ID `42571338`;
- branch `refs/heads/main`;
- event `workflow_dispatch`;
- environment `gcp-research`;
- exact workflow path `.github/workflows/deploy-gcp.yml@refs/heads/main`.

The IDs are public metadata, not secrets. Numeric IDs are used because account
and repository names can be renamed or, after deletion, potentially reused.

## Files introduced by this release path

| File | Purpose |
| --- | --- |
| `.github/workflows/deploy-gcp.yml` | Validates the request and CI result, waits at the protected environment, authenticates keylessly and controls the VM over IAP |
| `terraform/github-cd.tf` | Creates the OIDC provider, deployer service account and least-privilege IAM bindings |
| `scripts/release-vm.sh` | Performs the privileged, locked, SHA-based release on the VM |
| `tests/test_github_cd.py` | Prevents accidental push deployment, mutable action tags, long-lived credentials or weakened trust conditions |

## Before enabling CD

Complete these checks first:

1. The application is already healthy on `claims-research-vm`.
2. The VM checkout is `/opt/decentralized-claims-registry`.
3. `infrastructure/gcp/.env.gcp` exists on the VM with mode `600`.
4. `infrastructure/gcp/.env.gcp-secrets` exists on the VM.
5. The reviewed model artifact exists at the path selected by `.env.gcp`.
6. `main` has a successful **Continuous integration** workflow run.
7. The `production-cd-pipeline` pull request has been reviewed and merged.

Do not copy `.env.gcp`, `.env.gcp-secrets`, private keys, Terraform state, or
generated `gha-creds-*.json` files into GitHub.

## Step 1: configure the protected GitHub Environment

In GitHub, open:

```text
Repository -> Settings -> Environments -> New environment
```

Create an environment named exactly:

```text
gcp-research
```

Configure these protection rules before adding cloud access:

1. Add a required reviewer. If another trusted reviewer is available, enable
   **Prevent self-review**.
2. Restrict deployment branches to `main` only.
3. Disable administrator bypass if that option is available for the repository.
4. Optionally add a short wait timer to make accidental clicks cancellable.

Add these non-secret environment variables:

| Variable | Value |
| --- | --- |
| `GCP_CD_ENABLED` | Initially `false` |
| `GCP_PROJECT_ID` | `decentralized-claim-registry` |
| `GCP_ZONE` | `us-central1-a` |
| `GCP_VM_NAME` | `claims-research-vm` |

Keep `GCP_CD_ENABLED=false` until Terraform has created the identity and the two
remaining variables have been copied from Terraform outputs.

GitHub environment protection behavior is documented in the
[official GitHub environment guide](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

## Step 2: create the keyless GCP identity

On your Mac, start in the real repository:

```bash
cd /Users/ganesh/Documents/dissertation/Decentralized-Claims-Registry/infrastructure/gcp/terraform
```

In the ignored `terraform.tfvars`, add:

```hcl
enable_github_cd           = true
github_repository          = "ganesh1997oli/Decentralized-Claims-Registry"
github_repository_id       = "1265286953"
github_repository_owner_id = "42571338"
github_deploy_branch       = "main"
github_deploy_environment  = "gcp-research"
```

Format, validate and review the exact infrastructure change:

```bash
terraform fmt -check -recursive
terraform validate
terraform plan -out=/tmp/claims-github-cd.tfplan
```

The plan should add the following without replacing the VM:

- the OS Login API and four required Google identity APIs if Terraform does not
  already manage them;
- one Workload Identity Pool and one OIDC provider;
- one `claims-research-cd` service account;
- Workload Identity User on that service account;
- Compute Viewer, OS Admin Login and port-22-only IAP access;
- Service Account User on the VM's low-privilege observability identity.

Do not apply a plan that replaces the VM, address, disk, firewall, or existing VM
service account. After reviewing the plan:

```bash
terraform apply /tmp/claims-github-cd.tfplan
```

Terraform does not create a service-account key. Google recommends Workload
Identity Federation for deployment pipelines because it removes long-lived key
maintenance; see the
[official Google Cloud guide](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines).

Read the two non-secret outputs:

```bash
terraform output -raw github_workload_identity_provider
terraform output -raw github_deployer_service_account
```

## Step 3: finish the GitHub Environment

Return to `Settings -> Environments -> gcp-research` and add:

| Variable | Value |
| --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Exact `github_workload_identity_provider` output |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Exact `github_deployer_service_account` output |

Finally change:

```text
GCP_CD_ENABLED=true
```

No GitHub secret is required. The workflow requests a short-lived GitHub OIDC
token only after the environment gate and exchanges it for the narrowly scoped
GCP identity.

IAM and Workload Identity changes can take several minutes to propagate. Wait
five minutes before treating the first authentication failure as a defect.

## Step 4: perform the first release

The manual button appears only after the workflow is on the default branch:

```text
GitHub -> Actions -> Deploy reviewed commit to Google Cloud -> Run workflow
```

Choose `main`, then enter:

| Input | First release value |
| --- | --- |
| `release_sha` | Leave blank to deploy the selected current `main` SHA |
| `confirmation` | `DEPLOY` |

The prepare job rejects the request unless the SHA is a complete commit in
`origin/main` and the normal `ci.yml` workflow has succeeded for that exact SHA.
The deploy job then waits for the `gcp-research` approval.

The workflow does not start a stopped VM. If the VM is stopped, the release
fails before authentication is used for SSH; start the VM deliberately and run
the workflow again.

During the first release, the current VM may contain files copied manually with
`gcloud compute scp`. Before checking out the reviewed SHA, `release-vm.sh`
writes a private binary patch and status file under:

```text
/var/lib/claims-registry-deployments/backups/
```

The script never runs `git clean`, so ignored secrets, model artifacts and local
Terraform material remain. It then builds the reviewed images, runs Compose and
executes every read-only verification check.

## Normal release process

For later releases:

1. Work on a feature branch.
2. Open and review a pull request.
3. Merge it to `main`.
4. Wait for **Continuous integration** to succeed on the merge SHA.
5. Open the manual deployment workflow.
6. Leave `release_sha` blank, type `DEPLOY`, and request the release.
7. Approve the `gcp-research` environment job.
8. Review the GitHub step summary and the public application.

The VM records the last successful SHA and timestamp in root-only files under:

```text
/var/lib/claims-registry-deployments/
```

GitHub Actions remains the primary release log, including the initiating actor,
reviewed SHA, CI run and deployment result.

## Rollback and failed releases

If build, startup or verification fails, `release-vm.sh` attempts to restore the
previous code SHA and run deployment plus verification again. GitHub still marks
the requested release as failed even when that automatic rollback succeeds.

This is a **code rollback**, not a database downgrade. Migrations merged to
`main` must remain backward-compatible with the previous application release.
Destructive schema changes require a separate reviewed migration and recovery
plan.

To deliberately redeploy an earlier reviewed commit:

1. Find a full 40-character SHA in `main` history with successful CI.
2. Run the manual workflow.
3. Paste that SHA into `release_sha`.
4. Type `DEPLOY` and approve the environment job.

The workflow rejects feature-branch commits and abbreviated SHAs.

## Emergency disable procedure

To stop new GitHub deployments immediately, change the protected environment
variable:

```text
GCP_CD_ENABLED=false
```

That fails the workflow before cloud authentication. To revoke the GCP trust as
well, set `enable_github_cd=false` in the ignored `terraform.tfvars`, review a
Terraform plan, and apply only the removal of the GitHub pool, provider, deployer
service account and its IAM bindings.

Do not run `terraform destroy` merely to disable CD; that would also remove the
research VM and its boot disk.

## Troubleshooting

| Failure | Meaning and first check |
| --- | --- |
| Manual workflow is not visible | `deploy-gcp.yml` has not been merged to the default branch |
| No successful CI run | Wait for `ci.yml` on the exact release SHA or fix its failure |
| Missing protected variable | Finish configuring `gcp-research`; never replace it with a repository secret containing a key |
| OIDC attribute-condition failure | Confirm IDs, `main`, workflow path and environment name match Terraform exactly; allow IAM propagation time |
| IAP or OS Login denied | Review only the Terraform-managed deployer roles and the VM's `enable-oslogin=TRUE` metadata |
| VM is not RUNNING | Start it deliberately; the workflow will not create billable runtime |
| Deployment already running | Wait for the existing locked release to finish; do not delete the lock file while its process is active |
| Deploy failed but rollback passed | Fix the new commit, merge a new SHA and redeploy; do not retry an unchanged bad release |
| Deploy and rollback both failed | Connect through IAP, inspect Compose state and logs, and preserve VM evidence before changing anything |

The pipeline intentionally builds on the VM rather than publishing images to a
registry. That is appropriate for this dissertation appliance and keeps the
current secret mounts and Compose model unchanged. A later multi-host system
should build once in CI, sign images, store them in Artifact Registry, deploy by
immutable image digest, use managed databases, and implement independent schema
roll-forward/rollback procedures.
