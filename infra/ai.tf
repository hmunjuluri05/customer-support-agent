# AI Services (Microsoft Foundry) account hosting the chat, embedding and
# realtime voice deployments.
#
# custom_subdomain_name is required: without it the resource has no
# *.openai.azure.com host, and both Entra token auth and the realtime
# /openai/v1/realtime/client_secrets endpoint are unavailable.

resource "azurerm_cognitive_account" "ai" {
  name                  = "aoai-${local.prefix}"
  location              = azurerm_resource_group.main.location
  resource_group_name   = azurerm_resource_group.main.name
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = "aoai-${local.prefix}"

  # Keys remain enabled for local development convenience, but the deployed
  # application authenticates with managed identity (see identity.tf).
  local_auth_enabled            = true
  public_network_access_enabled = true

  tags = local.tags
}

# Deployments are created serially. Azure rejects concurrent deployment writes
# against one account with a conflict, so each depends on the previous.

resource "azurerm_cognitive_deployment" "chat" {
  name                 = "gpt-chat"
  cognitive_account_id = azurerm_cognitive_account.ai.id

  model {
    format  = "OpenAI"
    name    = var.model_chat.name
    version = var.model_chat.version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.model_chat.capacity
  }
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = "text-embedding-3-small"
  cognitive_account_id = azurerm_cognitive_account.ai.id

  model {
    format  = "OpenAI"
    name    = var.model_embedding.name
    version = var.model_embedding.version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.model_embedding.capacity
  }

  depends_on = [azurerm_cognitive_deployment.chat]
}

resource "azurerm_cognitive_deployment" "realtime" {
  name                 = "gpt-realtime-mini"
  cognitive_account_id = azurerm_cognitive_account.ai.id

  model {
    format  = "OpenAI"
    name    = var.model_realtime.name
    version = var.model_realtime.version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.model_realtime.capacity
  }

  depends_on = [azurerm_cognitive_deployment.embedding]
}
