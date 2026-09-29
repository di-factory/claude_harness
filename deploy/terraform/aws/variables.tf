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
  description = "small (0.5 vCPU, 1 GB, db.t4g.micro) or medium (1 vCPU, 2 GB, db.t4g.small)."
  type        = string
  default     = "small"
  validation {
    condition     = contains(["small", "medium"], var.size)
    error_message = "size must be small or medium."
  }
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
