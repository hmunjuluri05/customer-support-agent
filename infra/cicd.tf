# GitHub Actions access to this subscription, via OIDC federation.
#
# No secret is ever created, stored, or rotated. GitHub mints a short-lived
# token for a specific repository, branch or pull request, Entra exchanges it
# for an Azure token, and the subject line below is the only thing that
# decides which workflow run is allowed to do what. A leaked repository
# variable is a client id, which is not a credential.
#
# Two identities, not one, because the two jobs need unrelated powers:
#
#   * `ci`     calls Azure OpenAI to run the test suite. It can read nothing
#              else and write nothing at all.
#   * `deploy` builds images and rolls out revisions. It never gets inference
#              access, so a compromised deploy job cannot spend model quota.
#
# Everything here is created only when `github_repository` is set, so a
# reviewer who clones this and runs `terraform apply` gets the application
# without being asked for a GitHub account.

locals {
  gha_enabled = var.github_repository != null && var.github_repository != ""
  gha_count   = local.gha_enabled ? 1 : 0

  # GitHub's OIDC issuer and the audience Entra expects back.
  gha_issuer   = "https://token.actions.githubusercontent.com"
  gha_audience = ["api://AzureADTokenExchange"]

  # Two subject formats, because GitHub changed the one it sends.
  #
  # The classic subject is `repo:owner/repo:ref:refs/heads/main`. GitHub now
  # issues immutable identifiers instead -- `repo:owner@<owner-id>/repo@<repo-id>:
  # ref:refs/heads/main` -- and a credential written the classic way simply
  # does not match: Entra refuses with AADSTS700213 and the workflow fails at
  # `azure/login` with nothing wrong on the Azure side.
  #
  # Registering both costs nothing (an identity allows 20 credentials) and
  # means neither a rollout nor a rollback of that GitHub change breaks the
  # pipeline. The immutable pair is created only when the ids are supplied.
  gha_owner = split("/", coalesce(var.github_repository, "/"))[0]
  gha_repo  = split("/", coalesce(var.github_repository, "/"))[1]

  gha_immutable       = local.gha_enabled && var.github_owner_id != null && var.github_repository_id != null
  gha_immutable_count = local.gha_immutable ? 1 : 0
  gha_immutable_repo  = "${local.gha_owner}@${var.github_owner_id}/${local.gha_repo}@${var.github_repository_id}"
}

# ------------------------------------------------------------------- CI --

resource "azurerm_user_assigned_identity" "gha_ci" {
  count               = local.gha_count
  name                = "id-gha-ci-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  tags = local.tags
}

# Pull requests. Deliberately separate from the branch credential below: a PR
# from a fork runs with this subject and gets inference access only, never the
# deploy identity.
resource "azurerm_federated_identity_credential" "gha_ci_pr" {
  count     = local.gha_count
  name      = "github-pull-request"
  parent_id = azurerm_user_assigned_identity.gha_ci[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${var.github_repository}:pull_request"
}

resource "azurerm_federated_identity_credential" "gha_ci_main" {
  count     = local.gha_count
  name      = "github-main"
  parent_id = azurerm_user_assigned_identity.gha_ci[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${var.github_repository}:ref:refs/heads/${var.github_default_branch}"
}

# The test suite embeds real queries, because a zero-vector fake cannot produce
# a GROUNDED decision and the grounding gate is what most of those tests are
# about. Data-plane inference only: this cannot create or change a deployment.
resource "azurerm_role_assignment" "gha_ci_openai" {
  count                = local.gha_count
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.gha_ci[0].principal_id
}

# --------------------------------------------------------------- deploy --

resource "azurerm_user_assigned_identity" "gha_deploy" {
  count               = local.gha_count
  name                = "id-gha-deploy-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  tags = local.tags
}

# Only the default branch. A pull request cannot assume this identity, so
# untrusted code never reaches a credential that can push an image.
resource "azurerm_federated_identity_credential" "gha_deploy_main" {
  count     = local.gha_count
  name      = "github-main"
  parent_id = azurerm_user_assigned_identity.gha_deploy[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${var.github_repository}:ref:refs/heads/${var.github_default_branch}"
}

resource "azurerm_role_assignment" "gha_deploy_acr_push" {
  count                = local.gha_count
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.gha_deploy[0].principal_id
}

# `az acr build` does not just push: it schedules a build task inside the
# registry. AcrPush does not cover that, and the usual answer -- grant
# Contributor on the registry -- also hands over the ability to delete
# repositories and rotate admin credentials. This is the three actions that
# command actually needs.
resource "azurerm_role_definition" "acr_build" {
  count       = local.gha_count
  name        = "ACR Build Runner (${local.prefix})"
  scope       = azurerm_container_registry.main.id
  description = "Schedule and follow an az acr build run. No delete, no credential access."

  permissions {
    actions = [
      "Microsoft.ContainerRegistry/registries/scheduleRun/action",
      "Microsoft.ContainerRegistry/registries/runs/read",
      "Microsoft.ContainerRegistry/registries/runs/listLogSasUrl/action",
    ]
    not_actions = []
  }

  assignable_scopes = [azurerm_container_registry.main.id]
}

resource "azurerm_role_assignment" "gha_deploy_acr_build" {
  count              = local.gha_count
  scope              = azurerm_container_registry.main.id
  role_definition_id = azurerm_role_definition.acr_build[0].role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.gha_deploy[0].principal_id
}

# Scoped to the two apps rather than the resource group: this identity can roll
# out a revision and nothing else. It cannot touch the database, the registry's
# lifecycle, or the Azure OpenAI account.
resource "azurerm_role_assignment" "gha_deploy_api" {
  count                = local.gha_count
  scope                = azurerm_container_app.api.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.gha_deploy[0].principal_id
}

resource "azurerm_role_assignment" "gha_deploy_web" {
  count                = local.gha_count
  scope                = azurerm_container_app.web.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.gha_deploy[0].principal_id
}

# A revision runs as the application identity, so the deploy identity has to be
# allowed to assign it. Without this, `az containerapp update` fails with a
# LinkedAuthorizationFailed that names neither identity clearly.
resource "azurerm_role_assignment" "gha_deploy_app_identity" {
  count                = local.gha_count
  scope                = azurerm_user_assigned_identity.app.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = azurerm_user_assigned_identity.gha_deploy[0].principal_id
}

# ------------------------------------------- immutable-identifier subjects --
#
# Same three credentials as above, in the subject format GitHub actually
# presents today. See the note in `locals` for why both exist.

resource "azurerm_federated_identity_credential" "gha_ci_pr_immutable" {
  count     = local.gha_immutable_count
  name      = "github-pull-request-immutable"
  parent_id = azurerm_user_assigned_identity.gha_ci[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${local.gha_immutable_repo}:pull_request"
}

resource "azurerm_federated_identity_credential" "gha_ci_main_immutable" {
  count     = local.gha_immutable_count
  name      = "github-main-immutable"
  parent_id = azurerm_user_assigned_identity.gha_ci[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${local.gha_immutable_repo}:ref:refs/heads/${var.github_default_branch}"
}

resource "azurerm_federated_identity_credential" "gha_deploy_main_immutable" {
  count     = local.gha_immutable_count
  name      = "github-main-immutable"
  parent_id = azurerm_user_assigned_identity.gha_deploy[0].id
  issuer    = local.gha_issuer
  audience  = local.gha_audience
  subject   = "repo:${local.gha_immutable_repo}:ref:refs/heads/${var.github_default_branch}"
}
