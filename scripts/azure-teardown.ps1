<#
.SYNOPSIS
    Stops or permanently destroys all Azure resources for the Customer Support AI Agent demo.

.DESCRIPTION
    Three modes:

      Stop     Pauses the expensive always-on resources without losing anything.
               Container Apps scale to zero, PostgreSQL Flexible Server is stopped.
               Safe to run between demo rehearsals. Reverse it with -Mode Start.

      Start    Undoes Stop.

      Destroy  Deletes the whole resource group, then PURGES the soft-deleted
               AI Services / Azure OpenAI accounts and Key Vaults that Azure
               otherwise retains (and keeps charging model quota against) for days.
               This is the "interview is over" button. Irreversible.

    Deleting a resource group is NOT enough on its own: Cognitive Services
    (Azure OpenAI / AI Services) accounts enter a soft-deleted state and keep
    holding your model quota and resource name until they are purged.
    This script purges them.

.PARAMETER ResourceGroup
    One or more resource groups to act on. Defaults to rg-support-agent.

.PARAMETER SubscriptionId
    Optional. Switches subscription before acting. Defaults to your current az context.

.PARAMETER Mode
    Stop | Start | Destroy. Defaults to Destroy.

.PARAMETER ListOnly
    Print the inventory and exit without changing anything. Always safe.

.PARAMETER Force
    Skip the typed confirmation prompt in Destroy mode. For unattended use only.

.EXAMPLE
    .\scripts\azure-teardown.ps1 -ListOnly
    Show what exists and what would happen. Changes nothing.

.EXAMPLE
    .\scripts\azure-teardown.ps1 -Mode Stop
    Pause the demo overnight.

.EXAMPLE
    .\scripts\azure-teardown.ps1 -Mode Destroy
    Delete everything, then purge soft-deleted AI accounts.
#>
[CmdletBinding()]
param(
    [string[]] $ResourceGroup = @('rg-support-agent'),
    [string]   $SubscriptionId,
    [ValidateSet('Stop', 'Start', 'Destroy')]
    [string]   $Mode = 'Destroy',
    [switch]   $ListOnly,
    [switch]   $Force
)

$ErrorActionPreference = 'Stop'
$script:Failures = @()

function Write-Step { param([string] $Message) Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string] $Message) Write-Host "    [ok]   $Message" -ForegroundColor Green }
function Write-Note { param([string] $Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn { param([string] $Message) Write-Host "    [warn] $Message" -ForegroundColor Yellow }
function Write-Fail {
    param([string] $Message)
    Write-Host "    [FAIL] $Message" -ForegroundColor Red
    $script:Failures += $Message
}

# Runs az and returns parsed JSON. Throws on non-zero exit.
function Invoke-AzJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $AzArgs)
    $raw = & az @AzArgs --output json
    if ($LASTEXITCODE -ne 0) { throw "az $($AzArgs -join ' ') exited with $LASTEXITCODE" }
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    return ($raw | ConvertFrom-Json)
}

# Like Invoke-AzJson but returns $null instead of throwing. Used for optional
# lookups whose az extension (containerapp, postgres) may not be installed.
function Get-AzJsonSafe {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $AzArgs)
    try {
        return (Invoke-AzJson @AzArgs)
    }
    catch {
        Write-Warn "Skipped 'az $($AzArgs -join ' ')' - $($_.Exception.Message)"
        return $null
    }
}

# Runs az for its side effect. Returns $true on success, never throws.
function Invoke-AzVoid {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $AzArgs)
    & az @AzArgs --only-show-errors | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------- preflight --

Write-Step 'Preflight'

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI not found on PATH. Install it from https://aka.ms/installazurecli'
}

try {
    $account = Invoke-AzJson account show
}
catch {
    throw 'Not signed in to Azure. Run: az login'
}

if ($SubscriptionId) {
    if (-not (Invoke-AzVoid account set --subscription $SubscriptionId)) {
        throw "Could not switch to subscription $SubscriptionId"
    }
    $account = Invoke-AzJson account show
}

Write-Ok "Subscription: $($account.name)"
Write-Note "Id:           $($account.id)"
Write-Note "Mode:         $Mode"

# ---------------------------------------------------------------- inventory --

$targets = @()

foreach ($rg in $ResourceGroup) {
    Write-Step "Inventory: $rg"

    $exists = (& az group exists --name $rg --output tsv)
    if ($LASTEXITCODE -ne 0) {
        # Never treat a CLI/auth failure as "absent" - that would silently
        # report success while live resources keep billing.
        Write-Fail "Could not determine whether '$rg' exists (az exited $LASTEXITCODE). Re-run after 'az login'."
        continue
    }
    if ($exists -ne 'true') {
        Write-Warn "Resource group '$rg' does not exist. Skipping."
        continue
    }

    $resources = Get-AzJsonSafe resource list --resource-group $rg --query '[].{Name:name, Type:type, Location:location}'

    if (-not $resources) {
        Write-Note 'Resource group is empty.'
    }
    else {
        $resources | Sort-Object Type, Name | Format-Table -AutoSize | Out-String -Width 160 | Write-Host
    }

    # Captured BEFORE deletion: purge needs name + location after the group is gone.
    $aiAccounts = Get-AzJsonSafe cognitiveservices account list --resource-group $rg --query '[].{Name:name, Location:location}'
    $vaults     = Get-AzJsonSafe keyvault list --resource-group $rg --query '[].{Name:name, Location:location}'
    $apps       = Get-AzJsonSafe containerapp list --resource-group $rg --query '[].name'
    $servers    = Get-AzJsonSafe postgres flexible-server list --resource-group $rg --query '[].name'

    $targets += [pscustomobject]@{
        Name          = $rg
        ResourceCount = @($resources).Count
        AiAccounts    = @($aiAccounts)
        Vaults        = @($vaults)
        ContainerApps = @($apps)
        PostgresNames = @($servers)
    }
}

if ($targets.Count -eq 0) {
    Write-Step 'Nothing to do'
    if ($script:Failures.Count -gt 0) {
        Write-Host 'Could not inspect every resource group - see failures above.' -ForegroundColor Red
        exit 1
    }
    Write-Host 'No matching resource groups found. Everything is already gone.' -ForegroundColor Green
    exit 0
}

if ($ListOnly) {
    Write-Step 'List-only mode'
    Write-Host 'No changes made. Re-run without -ListOnly to act.' -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------- stop/start --

if ($Mode -eq 'Stop' -or $Mode -eq 'Start') {

    $minReplicas = '0'
    if ($Mode -eq 'Start') { $minReplicas = '1' }

    foreach ($t in $targets) {
        Write-Step "$Mode : $($t.Name)"

        foreach ($app in $t.ContainerApps) {
            if (Invoke-AzVoid containerapp update --name $app --resource-group $t.Name --min-replicas $minReplicas) {
                Write-Ok "Container app '$app' min-replicas = $minReplicas"
            }
            else {
                Write-Fail "Could not update container app '$app'"
            }
        }

        foreach ($server in $t.PostgresNames) {
            $verb = 'stop'
            if ($Mode -eq 'Start') { $verb = 'start' }

            if (Invoke-AzVoid postgres flexible-server $verb --name $server --resource-group $t.Name) {
                Write-Ok "PostgreSQL '$server' $verb requested"
            }
            else {
                Write-Fail "Could not $verb PostgreSQL '$server'"
            }
        }

        if ($t.AiAccounts.Count -gt 0) {
            Write-Note 'AI Services accounts bill per request; idle costs nothing. Left running.'
        }
    }

    if ($Mode -eq 'Stop') {
        Write-Step 'Stopped'
        Write-Note 'Storage for the stopped database is still billed.'
        Write-Note 'Azure auto-restarts a stopped Flexible Server after 7 days.'
        Write-Note 'Resume with: .\scripts\azure-teardown.ps1 -Mode Start'
    }
    else {
        Write-Step 'Started'
        Write-Note 'Give the database a minute, then restart or redeploy the container apps.'
    }
}

# ------------------------------------------------------------------ destroy --

if ($Mode -eq 'Destroy') {

    Write-Step 'About to PERMANENTLY DELETE'

    foreach ($t in $targets) {
        Write-Host "    Resource group '$($t.Name)' - $($t.ResourceCount) resource(s)" -ForegroundColor Red
        foreach ($a in $t.AiAccounts) { Write-Host "      + purge AI account '$($a.Name)'" -ForegroundColor Red }
        foreach ($v in $t.Vaults)     { Write-Host "      + purge key vault  '$($v.Name)'" -ForegroundColor Red }
    }

    Write-Host ''
    Write-Host '    This cannot be undone. Databases and their backups go with it.' -ForegroundColor Yellow

    if (-not $Force) {
        $expected = $targets[0].Name
        Write-Host ''
        $typed = Read-Host "    Type '$expected' to confirm"
        if ($typed -ne $expected) {
            Write-Host ''
            Write-Host 'Confirmation did not match. Nothing was deleted.' -ForegroundColor Green
            return
        }
    }

    foreach ($t in $targets) {
        Write-Step "Deleting resource group: $($t.Name)"
        Write-Note 'Usually 5-15 minutes. Waiting, so the purges can follow.'

        if (Invoke-AzVoid group delete --name $t.Name --yes) {
            Write-Ok "Resource group '$($t.Name)' deleted"
        }
        else {
            Write-Fail "Could not delete resource group '$($t.Name)'. Skipping its purges."
            continue
        }

        foreach ($a in $t.AiAccounts) {
            if (Invoke-AzVoid cognitiveservices account purge --name $a.Name --resource-group $t.Name --location $a.Location) {
                Write-Ok "Purged AI account '$($a.Name)' - model quota released"
            }
            else {
                Write-Fail "Could not purge AI account '$($a.Name)' in $($a.Location)"
            }
        }

        foreach ($v in $t.Vaults) {
            if (Invoke-AzVoid keyvault purge --name $v.Name --location $v.Location) {
                Write-Ok "Purged key vault '$($v.Name)'"
            }
            else {
                Write-Warn "Could not purge key vault '$($v.Name)' (purge protection may be enabled)"
            }
        }
    }

    # Catch anything soft-deleted by an earlier run that never got purged.
    Write-Step 'Checking for leftover soft-deleted AI accounts'
    try {
        $orphans = Invoke-AzJson cognitiveservices account list-deleted --query '[].{Name:name, Location:location}'
    }
    catch {
        $orphans = $null
        Write-Warn 'Could not query soft-deleted accounts.'
    }

    if (-not $orphans -or @($orphans).Count -eq 0) {
        Write-Ok 'None. Model quota is fully released.'
    }
    else {
        Write-Warn 'These remain soft-deleted and still hold quota:'
        foreach ($o in $orphans) {
            Write-Host "      $($o.Name)  [$($o.Location)]" -ForegroundColor Yellow
            Write-Host "        az cognitiveservices account purge --name $($o.Name) --location $($o.Location) --resource-group <rg>" -ForegroundColor DarkGray
        }
    }
}

# ------------------------------------------------------------------ summary --

Write-Step 'Summary'

if ($script:Failures.Count -eq 0) {
    Write-Host "Completed with no errors. Mode: $Mode" -ForegroundColor Green
    if ($Mode -eq 'Destroy') {
        Write-Host 'Verify spend has stopped: https://portal.azure.com/#view/Microsoft_Azure_CostManagement' -ForegroundColor DarkGray
    }
    exit 0
}
else {
    Write-Host "Completed with $($script:Failures.Count) failure(s):" -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
