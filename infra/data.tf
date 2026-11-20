resource "random_password" "postgres" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "azurerm_postgresql_flexible_server" "main" {
  name = "pg-${local.prefix}"

  # Deliberately NOT azurerm_resource_group.main.location: this subscription is
  # blocked from provisioning Flexible Server in eastus2. See var.postgres_location.
  location            = var.postgres_location
  resource_group_name = azurerm_resource_group.main.name

  version                      = "16"
  sku_name                     = var.postgres_sku
  storage_mb                   = var.postgres_storage_mb
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false
  zone                         = "1"

  administrator_login    = var.postgres_admin_login
  administrator_password = random_password.postgres.result

  # Public endpoint with a firewall restricted to Azure services. A private
  # endpoint plus delegated subnet would be the production choice; it is not
  # used here because Container Apps Consumption egress would then require
  # VNet integration, which is out of scope for this demo.
  public_network_access_enabled = true

  tags = local.tags

  lifecycle {
    # Azure assigns a zone when one is not pinned; avoid spurious replacement.
    ignore_changes = [zone, high_availability[0].standby_availability_zone]
  }
}

# The 0.0.0.0-0.0.0.0 range is the documented special case meaning
# "allow Azure services and resources to access this server".
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# pgvector is not enabled by default. It must be allowlisted at the server level
# before `CREATE EXTENSION vector` will succeed inside the database.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_database" "app" {
  name      = "support_agent"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"

  lifecycle {
    prevent_destroy = false
  }
}

# Optional developer access, for migrations and seeding from a workstation.
# The Azure-services rule above does not cover traffic from outside Azure.
resource "azurerm_postgresql_flexible_server_firewall_rule" "dev_client" {
  count = var.dev_client_ip == "" ? 0 : 1

  name             = "AllowDevClient"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = var.dev_client_ip
  end_ip_address   = var.dev_client_ip
}
