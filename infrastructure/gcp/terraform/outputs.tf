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
