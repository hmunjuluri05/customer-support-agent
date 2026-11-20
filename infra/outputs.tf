output "demo_url" {
  description = "Public HTTPS URL of the application. This is the reviewer-facing link."
  value       = "https://${azurerm_container_app.web.ingress[0].fqdn}"
}

output "api_internal_fqdn" {
  description = "Internal-only FQDN of the API, reachable within the Container Apps environment."
  value       = azurerm_container_app.api.ingress[0].fqdn
}

output "acr_login_server" {
  description = "Registry host used by the az acr build / containerapp update deploy loop."
  value       = azurerm_container_registry.main.login_server
}

output "azure_openai_endpoint" {
  description = "Azure OpenAI endpoint, including the realtime client_secrets path."
  value       = "https://${azurerm_cognitive_account.ai.custom_subdomain_name}.openai.azure.com/"
}

output "model_deployments" {
  description = "Application-facing deployment names and the models behind them."
  value = {
    chat      = "${azurerm_cognitive_deployment.chat.name} -> ${var.model_chat.name} ${var.model_chat.version}"
    embedding = "${azurerm_cognitive_deployment.embedding.name} -> ${var.model_embedding.name} ${var.model_embedding.version}"
    realtime  = "${azurerm_cognitive_deployment.realtime.name} -> ${var.model_realtime.name} ${var.model_realtime.version}"
  }
}

output "postgres_fqdn" {
  description = "PostgreSQL Flexible Server host."
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "database_url" {
  description = "SQLAlchemy connection URL. Used for local migrations and seeding."
  value       = local.postgres_url
  sensitive   = true
}

output "managed_identity_client_id" {
  description = "Client ID of the identity used for ACR pull and Azure OpenAI access."
  value       = azurerm_user_assigned_identity.app.client_id
}
