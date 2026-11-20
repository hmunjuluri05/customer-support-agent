terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Local state by design. A remote backend would itself need a storage account,
  # which could not be created by this root module without a bootstrap step
  # outside Terraform. Uncomment only if you first create that account by other
  # means and accept the exception.
  #
  # backend "azurerm" {
  #   resource_group_name  = "rg-tfstate"
  #   storage_account_name = "sttfstatesupportagent"
  #   container_name       = "tfstate"
  #   key                  = "support-agent.tfstate"
  #   use_azuread_auth     = true
  # }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  # Microsoft.App is not registered by default on this subscription and is not
  # part of the provider's "core" set. Registering it here keeps subscription
  # setup inside Terraform rather than requiring a manual `az provider register`.
  resource_providers_to_register = ["Microsoft.App"]

  features {
    # Deleting a Cognitive Services account leaves it soft-deleted, holding both
    # its name and its model quota. This makes `terraform destroy` fully release
    # them, so a later `apply` can recreate the same names.
    cognitive_account {
      purge_soft_delete_on_destroy = true
    }

    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}

resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location

  tags = local.tags
}

locals {
  prefix = var.name_prefix

  tags = {
    project     = "customer-support-agent"
    environment = "demo"
    owner       = "interview-assessment"
    managed_by  = "terraform"
  }
}
