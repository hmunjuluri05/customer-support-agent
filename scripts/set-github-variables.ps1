<#
.SYNOPSIS
    Copy the CI/CD values Terraform produced into GitHub Actions variables.

.DESCRIPTION
    Reads `terraform output -json github_actions_variables` and sets each entry
    as a repository variable with the GitHub CLI.

    These are variables, not secrets. A client id identifies an identity; it
    does not authenticate as one. Only a workflow run whose OIDC subject
    matches the federated credential in infra/cicd.tf can exchange it for an
    Azure token, which is the whole point of using OIDC instead of storing a
    service principal password.

.EXAMPLE
    ./scripts/set-github-variables.ps1 -Repository hmunjuluri05/customer-support-agent
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Repository,

    [string] $InfraPath = (Join-Path $PSScriptRoot '..' 'infra')
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "The GitHub CLI is required. Install it from https://cli.github.com and run 'gh auth login'."
}

Push-Location $InfraPath
try {
    $raw = terraform output -json github_actions_variables
}
finally {
    Pop-Location
}

$values = $raw | ConvertFrom-Json

if (-not $values.PSObject.Properties.Name) {
    throw "No CI/CD outputs. Set github_repository in terraform.tfvars and apply first."
}

foreach ($entry in $values.PSObject.Properties) {
    Write-Host ("setting {0}" -f $entry.Name)
    gh variable set $entry.Name --repo $Repository --body $entry.Value
    if ($LASTEXITCODE -ne 0) {
        throw ("Failed to set {0}" -f $entry.Name)
    }
}

Write-Host ""
Write-Host "Done. Push to main and the Deploy workflow will run the test suite," -ForegroundColor Green
Write-Host "build both images, roll them out by digest, and smoke-test the URL." -ForegroundColor Green
