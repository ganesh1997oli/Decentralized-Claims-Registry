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

variable "enable_github_cd" {
  description = "Create the keyless, main-only GitHub Actions deployment identity. Keep false until the workflow has been reviewed and merged."
  type        = bool
  default     = false
}

variable "github_repository" {
  description = "Canonical owner/repository name used in the exact workflow-ref trust condition."
  type        = string
  default     = "ganesh1997oli/Decentralized-Claims-Registry"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must have the form owner/repository."
  }
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID; unlike a name, this value cannot be recycled after a rename."
  type        = string
  default     = "1265286953"

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_id))
    error_message = "github_repository_id must be a positive numeric GitHub repository ID."
  }
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub owner ID used to prevent owner-name recycling attacks."
  type        = string
  default     = "42571338"

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be a positive numeric GitHub account ID."
  }
}

variable "github_deploy_branch" {
  description = "Only this reviewed branch may exchange a GitHub OIDC token for the deployment identity."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.github_deploy_branch))
    error_message = "github_deploy_branch contains unsupported characters."
  }
}

variable "github_deploy_environment" {
  description = "GitHub Environment that owns the manual approval gate and non-secret GCP variables."
  type        = string
  default     = "gcp-research"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.github_deploy_environment))
    error_message = "github_deploy_environment contains unsupported characters."
  }
}
