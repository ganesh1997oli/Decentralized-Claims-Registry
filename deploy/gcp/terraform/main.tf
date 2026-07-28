provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  required_services = toset([
    "compute.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ])
}

# Enabling only the APIs used here keeps the project understandable and limits
# accidental access to unrelated paid services.
resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# The VM identity can write logs and metrics, but it cannot administer the
# project. Application credentials remain in the ignored .env.gcp file.
resource "google_service_account" "vm" {
  project      = var.project_id
  account_id   = "${var.deployment_name}-vm"
  display_name = "Claims research VM observability"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# Only the browser entry point is public. Kafka, PostgreSQL, FastAPI and metrics
# remain unexposed because Compose binds them to Docker or VM loopback.
resource "google_compute_firewall" "http" {
  project = var.project_id
  name    = "${var.deployment_name}-allow-http"
  network = var.network_name

  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.deployment_name}-web"]

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }

  depends_on = [google_project_service.required]
}

# SSH is reachable only through Google's Identity-Aware Proxy address range,
# avoiding an internet-wide port 22 rule. `gcloud compute ssh --tunnel-through-iap`
# uses this path.
resource "google_compute_firewall" "iap_ssh" {
  project = var.project_id
  name    = "${var.deployment_name}-allow-iap-ssh"
  network = var.network_name

  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["${var.deployment_name}-ssh"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_instance" "research" {
  project      = var.project_id
  name         = "${var.deployment_name}-vm"
  zone         = var.zone
  machine_type = var.machine_type

  # This is a disposable research host. Terraform can stop it during a safe
  # configuration update and deletes the boot disk when the VM is destroyed.
  allow_stopping_for_update = true
  deletion_protection       = false
  tags = [
    "${var.deployment_name}-web",
    "${var.deployment_name}-ssh",
  ]
  labels = {
    application = "decentralized-claims"
    environment = "research"
  }

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_size_gb
      type  = "pd-standard"
    }
  }

  network_interface {
    network = var.network_name

    # An ephemeral address avoids reserving a paid, unused static address after
    # the VM is stopped. It may change after a stop/start cycle.
    access_config {}
  }

  metadata = {
    enable-oslogin = "TRUE"
  }
  metadata_startup_script = file("${path.module}/../scripts/bootstrap-vm.sh")

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    preemptible         = false
  }

  depends_on = [
    google_project_iam_member.log_writer,
    google_project_iam_member.metric_writer,
  ]
}
