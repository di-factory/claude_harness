variable "name" {
  description = "Instance id (solution.id), used to name every resource."
  type        = string
}

variable "tenant_id" {
  description = "Tenant id; secrets live under dif/<tenant_id>/<name>/."
  type        = string
}

variable "region" {
  description = "AWS region, e.g. mx-central-1 for data residency in Mexico."
  type        = string
}

variable "image_tag" {
  description = "Tag of the instance image pushed to the ECR repository."
  type        = string
}

variable "secret_names" {
  description = "Secrets the instance reads (names only; the client sets the values)."
  type        = list(string)
}

variable "size" {
  description = <<-EOT
    Sizing profile (spec deploy.profile):
      small  - 0.5 vCPU / 1 GB, 1 task, db.t4g.micro, 7-day backups
      medium - 1 vCPU / 2 GB, 1 task, db.t4g.small, 14-day backups
      large  - 2 vCPU / 4 GB, 2 tasks, db.t4g.medium Multi-AZ, 30-day backups, private tasks
  EOT
  type        = string
  default     = "small"
  validation {
    condition     = contains(["small", "medium", "large"], var.size)
    error_message = "size must be small, medium or large."
  }
}

variable "private_tasks" {
  description = "Run tasks in private subnets behind a NAT gateway (default: only for large)."
  type        = bool
  default     = null
}

variable "alarm_email" {
  description = "Where CloudWatch alarms are sent (empty: the SNS topic has no subscriber yet)."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "How long container logs are kept (the audit trail lives in the database)."
  type        = number
  default     = 30
}

variable "images_to_keep" {
  description = "Images kept in ECR, so earlier releases stay available for rollback."
  type        = number
  default     = 20
}

variable "fleet_url" {
  description = "The Di-Factory control plane (outbound only); empty disables the instance agent."
  type        = string
  default     = ""
}

variable "fleet_public_key" {
  description = "The control plane's Ed25519 public key (base64); offers it did not sign are refused."
  type        = string
  default     = ""
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint for traces (empty: no telemetry leaves the instance)."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Required for production: channel providers sign requests against the public https URL."
  type        = string
  default     = ""
}

variable "public_url" {
  description = "The https URL providers call (e.g. https://citas.cliente.mx); used for signature checks."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Protect the database from deletion (turn off only for test accounts)."
  type        = bool
  default     = true
}
