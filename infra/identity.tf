# One user-assigned identity shared by both container apps. It pulls images from
# ACR and authenticates to Azure OpenAI, so no provider key is ever placed in
# application configuration.

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  tags = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Grants data-plane inference access only. It does not permit creating or
# modifying model deployments.
resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Data-plane access for the operator running ingestion and calibration locally.
#
# Owner grants control-plane `actions` but no `dataActions`, so a subscription
# Owner still cannot call the inference API. Without this, local ingestion must
# fall back to an API key, which defeats the keyless design.
data "azurerm_client_config" "current" {}

resource "azurerm_role_assignment" "operator_openai_user" {
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
}
