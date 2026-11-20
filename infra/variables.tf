variable "subscription_id" {
  description = "Azure subscription ID. Required by the azurerm 4.x provider."
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group that holds every resource in this build."
  type        = string
  default     = "rg-support-agent"
}

variable "location" {
  description = "Azure region. Must support the realtime model deployments below."
  type        = string
  default     = "eastus2"
}

variable "postgres_location" {
  description = <<-EOT
    Region for PostgreSQL Flexible Server, kept separate from var.location.

    This subscription is restricted from provisioning Flexible Server in
    eastus2, eastus, westus2, southcentralus and westeurope ("Subscriptions are
    restricted from provisioning in this region"), which is a Visual Studio
    subscription geography limit rather than a capacity shortage. Verified
    available: centralus, westus, westus3, northcentralus, canadacentral,
    northeurope, uksouth.

    centralus is the closest allowed region to eastus2, adding roughly 20-25 ms
    to each query. Acceptable for a fixture-sized corpus; recorded as a
    trade-off in the README.
  EOT
  type        = string
  default     = "centralus"
}

variable "name_prefix" {
  description = "Prefix for resource names. Must be globally unique where Azure requires it."
  type        = string
  default     = "supportagent-hk"
}

# ---------------------------------------------------------------- AI models --
# Verified available on this subscription in eastus2 on 2026-11-19.
# Deployment names are stable application-facing identifiers; the underlying
# model can be swapped here without touching application code.

variable "model_realtime" {
  description = "Realtime voice model backing the 'gpt-realtime-mini' deployment."
  type = object({
    name     = string
    version  = string
    capacity = number
  })
  default = {
    name     = "gpt-realtime-2.1-mini"
    version  = "2026-07-07"
    capacity = 5
  }
}

variable "model_chat" {
  description = "Text model backing the 'gpt-chat' deployment."
  type = object({
    name     = string
    version  = string
    capacity = number
  })
  default = {
    name     = "gpt-4.1-mini"
    version  = "2025-04-14"
    capacity = 30
  }
}

variable "model_embedding" {
  description = "Embedding model. Dimensionality is asserted in the database schema."
  type = object({
    name     = string
    version  = string
    capacity = number
  })
  default = {
    name     = "text-embedding-3-small"
    version  = "1"
    capacity = 30
  }
}

# --------------------------------------------------------------- PostgreSQL --

variable "postgres_admin_login" {
  description = "Administrator login for PostgreSQL Flexible Server."
  type        = string
  default     = "supportadmin"
}

variable "postgres_sku" {
  description = "Flexible Server SKU. Burstable is sufficient for a fixture-sized corpus."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "Storage in MB. 32768 is the smallest supported value."
  type        = number
  default     = 32768
}

# ------------------------------------------------------------ Container Apps --

variable "container_cpu" {
  description = "vCPU per replica."
  type        = number
  default     = 0.5
}

variable "container_memory" {
  description = "Memory per replica. Must pair with container_cpu per ACA rules."
  type        = string
  default     = "1Gi"
}

variable "replicas" {
  description = <<-EOT
    Fixed at 1/1 deliberately. min=1 avoids a cold start while a reviewer waits;
    max=1 removes cross-replica turn ownership, leases and fencing from scope.
    Documented as a trade-off in the README.
  EOT
  type = object({
    min = number
    max = number
  })
  default = {
    min = 1
    max = 1
  }
}

variable "placeholder_image" {
  description = <<-EOT
    Image used on first apply, before any application image exists in ACR.
    The container_app image attribute is excluded from lifecycle changes, so
    later `az containerapp update` deploys are not reverted by Terraform.
  EOT
  type        = string
  default     = "mcr.microsoft.com/k8se/quickstart:latest"
}

variable "grounding_threshold" {
  description = <<-EOT
    Minimum cosine similarity (1 - cosine_distance) for a GROUNDED decision.

    Calibrated 2026-11-19 against fixtures/eval/queries.json (30 labelled
    queries) by sweeping threshold x margin: recall 94%, out-of-scope
    rejection 100%, ambiguous handled 100%. Ties were broken toward the
    stricter value, since an unsupported policy claim is a worse failure than
    an unnecessary clarifying question.
  EOT
  type        = number
  default     = 0.38
}

variable "grounding_weak_margin" {
  description = <<-EOT
    Band below the threshold treated as WEAK -- ask a clarifying question --
    rather than UNSUPPORTED, which escalates. Calibrated jointly with the
    threshold above.
  EOT
  type        = number
  default     = 0.08
}

variable "dev_client_ip" {
  description = <<-EOT
    Public IP allowed to reach PostgreSQL directly, for running Alembic
    migrations and the fixture seed from a workstation. Empty creates no rule,
    leaving only the Azure-services rule in place.
  EOT
  type        = string
  default     = ""
}
