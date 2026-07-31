output "vm_name" {
  description = "Compute Engine instance name."
  value       = google_compute_instance.research.name
}

output "external_ip" {
  description = "Temporary browser address. Update FRONTEND_ORIGINS after a restart."
  value       = google_compute_instance.research.network_interface[0].access_config[0].nat_ip
}

output "application_url" {
  description = "Public HTTP URL for the dissertation demonstration."
  value       = "http://${google_compute_instance.research.network_interface[0].access_config[0].nat_ip}"
}

output "iap_ssh_command" {
  description = "Secure SSH command that does not expose port 22 to the internet."
  value       = "gcloud compute ssh ${google_compute_instance.research.name} --project ${var.project_id} --zone ${var.zone} --tunnel-through-iap"
}
