resource "azurerm_container_registry" "main" {
  name                = replace("acr${local.prefix}", "-", "")
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"

  # Image pulls use the managed identity in identity.tf, not an admin password.
  admin_enabled = false

  tags = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30

  tags = local.tags
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.prefix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  # Azure creates this profile implicitly. Declaring it keeps `terraform plan`
  # clean; omitting it makes Terraform try to remove it on every run.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
    minimum_count         = 0
    maximum_count         = 0
  }

  tags = local.tags
}

locals {
  postgres_url = join("", [
    "postgresql+psycopg://",
    var.postgres_admin_login,
    ":",
    urlencode(random_password.postgres.result),
    "@",
    azurerm_postgresql_flexible_server.main.fqdn,
    ":5432/",
    azurerm_postgresql_flexible_server_database.app.name,
    "?sslmode=require",
  ])
}

# ----------------------------------------------------------------- API app --
# Internal ingress only: not routable from the internet. The web app reaches it
# over the environment's internal DNS, which gives a single public origin and
# removes CORS and cross-site cookie concerns entirely.

resource "azurerm_container_app" "api" {
  name                         = "ca-api-${local.prefix}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  secret {
    name  = "database-url"
    value = local.postgres_url
  }

  ingress {
    external_enabled = false
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.replicas.min
    max_replicas = var.replicas.max

    container {
      name   = "api"
      image  = var.placeholder_image
      cpu    = var.container_cpu
      memory = var.container_memory

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }

      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = "https://${azurerm_cognitive_account.ai.custom_subdomain_name}.openai.azure.com/"
      }

      env {
        name  = "AZURE_OPENAI_CHAT_DEPLOYMENT"
        value = azurerm_cognitive_deployment.chat.name
      }

      env {
        name  = "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"
        value = azurerm_cognitive_deployment.embedding.name
      }

      env {
        name  = "AZURE_OPENAI_REALTIME_DEPLOYMENT"
        value = azurerm_cognitive_deployment.realtime.name
      }

      # Consumed by DefaultAzureCredential to select the user-assigned identity.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      # The policy corpus and account fixtures are baked into the image at
      # this path; the ingestion and seeding steps in entrypoint.sh read them.
      env {
        name  = "FIXTURES_DIR"
        value = "/app/fixtures"
      }

      env {
        name  = "GROUNDING_THRESHOLD"
        value = tostring(var.grounding_threshold)
      }

      env {
        name  = "GROUNDING_WEAK_MARGIN"
        value = tostring(var.grounding_weak_margin)
      }
    }
  }

  tags = local.tags

  lifecycle {
    # Image tags are advanced by `az containerapp update` in the deploy loop.
    # Terraform owns infrastructure, not the release.
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_role_assignment.openai_user,
  ]
}

# ----------------------------------------------------------------- Web app --
# The only public surface. This FQDN is the URL submitted to the reviewer.

resource "azurerm_container_app" "web" {
  name                         = "ca-web-${local.prefix}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  ingress {
    external_enabled = true
    target_port      = 3000

    # Explicitly HTTP/1.1. "auto" permits ACA to negotiate h2c to the
    # container, which hangs against servers that only speak HTTP/1.1
    # (uvicorn/h11, and Next.js standalone).
    transport = "http"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.replicas.min
    max_replicas = var.replicas.max

    container {
      name   = "web"
      image  = var.placeholder_image
      cpu    = var.container_cpu
      memory = var.container_memory

      # Server-side rewrite target for /api/*. Internal environment DNS.
      #
      # https, not http: an internal ingress still answers plain HTTP with a
      # 301 to its own https URL unless allow_insecure_connections is set. The
      # proxy would then forward that redirect to the browser, pointing at a
      # hostname only resolvable inside the environment -- so every API call
      # fails, and the internal FQDN leaks on the way. The environment's
      # certificate is valid for this name and verifies from inside the
      # container, so keeping TLS end to end costs nothing.
      env {
        name  = "API_INTERNAL_URL"
        value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
      }
    }
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [azurerm_role_assignment.acr_pull]
}
