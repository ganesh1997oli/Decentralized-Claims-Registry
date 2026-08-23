output "vm_name" {
  description = "Compute Engine instance name."
  value       = google_compute_instance.research.name
}

output "external_ip" {
  description = "Reserved public address used by the HTTPS hostname."
  value       = google_compute_address.public.address
}

output "public_host" {
  description = "Set PUBLIC_HOST and the claimant-auth host settings to this value."
  value       = local.public_host
}

output "application_url" {
  description = "Public HTTPS URL for the dissertation demonstration."
  value       = "https://${local.public_host}"
}

output "iap_ssh_command" {
  description = "Secure SSH command that does not expose port 22 to the internet."
  value       = "gcloud compute ssh ${google_compute_instance.research.name} --project ${var.project_id} --zone ${var.zone} --tunnel-through-iap"
}

output "github_cd_enabled" {
  description = "Whether this Terraform apply manages the keyless GitHub deployment identity."
  value       = var.enable_github_cd
}

output "github_workload_identity_provider" {
  description = "Set this non-secret value as GCP_WORKLOAD_IDENTITY_PROVIDER in the protected GitHub Environment."
  value       = try(google_iam_workload_identity_pool_provider.github[0].name, null)
}

output "github_deployer_service_account" {
  description = "Set this non-secret value as GCP_DEPLOY_SERVICE_ACCOUNT in the protected GitHub Environment."
  value       = try(google_service_account.github_deployer[0].email, null)
}
