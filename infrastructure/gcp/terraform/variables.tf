variable "project_id" {
  description = "Google Cloud project that owns the short-lived research VM."
  type        = string
}

variable "region" {
  description = "Region used for provider operations."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Compute Engine zone. Keep it in the selected region."
  type        = string
  default     = "us-central1-a"
}

variable "deployment_name" {
  description = "Readable prefix for the VM, firewall rules and service account."
  type        = string
  default     = "claims-research"
}

variable "machine_type" {
  description = "Two vCPUs and 8 GiB is comfortable for Kafka, SHAP and PostgreSQL."
  type        = string
  default     = "e2-standard-2"
}

variable "boot_disk_size_gb" {
  description = "Standard persistent disk size. Application logs are also capped."
  type        = number
  default     = 30

  validation {
    condition     = var.boot_disk_size_gb >= 20
    error_message = "The boot disk must be at least 20 GiB."
  }
}

variable "network_name" {
  description = "Existing VPC network used by the research VM."
  type        = string
  default     = "default"
}

variable "public_host" {
  description = "Optional custom DNS hostname pointing at the reserved IP. Empty uses the generated sslip.io hostname."
  type        = string
  default     = ""

  validation {
    condition = (
      var.public_host == "" ||
      can(regex("^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$", var.public_host))
    )
    error_message = "public_host must be empty or a hostname without a scheme, path, port or whitespace."
  }
}
