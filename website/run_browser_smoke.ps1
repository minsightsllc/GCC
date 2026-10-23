[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [Parameter(Mandatory = $true)]
    [string]$ReportPath,
    [string]$ArtifactDirectory = "",
    [switch]$Headed,
    [switch]$AllowRemote
)

$ErrorActionPreference = "Stop"
$websiteRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactRoot = if ($ArtifactDirectory) {
    [IO.Path]::GetFullPath($ArtifactDirectory)
} else {
    Join-Path $websiteRoot "..\output\playwright\certification"
}
New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
$resolvedReportPath = [IO.Path]::GetFullPath($ReportPath)
$reportParent = Split-Path -Parent $resolvedReportPath
if ($reportParent) {
    New-Item -ItemType Directory -Path $reportParent -Force | Out-Null
}

$npx = (Get-Command npx -ErrorAction Stop).Source
$session = "gcc-cert-" + [Guid]::NewGuid().ToString("N")
$checks = [System.Collections.Generic.List[object]]::new()
$failures = [System.Collections.Generic.List[object]]::new()
$base = $BaseUrl.TrimEnd("/")
$projectPath = "/dashboard/projects/$ProjectId/operations/"

if ($base -notmatch '^https?://(127\.0\.0\.1|localhost)(:\d+)?$') {
    if (-not $AllowRemote -or $env:GCC_CERTIFICATION_TARGET -ne "staging") {
        throw "Remote browser smoke requires -AllowRemote and GCC_CERTIFICATION_TARGET=staging."
    }
}

function Add-Check {
    param([string]$Role, [string]$Path, [string]$Description)
    $checks.Add([ordered]@{
        role = $Role
        path = $Path
        description = $Description
        passed = $true
    })
}

function Fail-Check {
    param([string]$Role, [string]$Path, [string]$Description, [string]$ErrorMessage)
    $failures.Add([ordered]@{
        role = $Role
        path = $Path
        description = $Description
        passed = $false
        error = $ErrorMessage
    })
    throw $ErrorMessage
}

function Invoke-Pw {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $all = @("--yes", "--package", "@playwright/cli", "playwright-cli", "--session", $session) + $Arguments
    & $npx @all
    if ($LASTEXITCODE -ne 0) {
        throw "playwright-cli failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

function Invoke-PwCode {
    param([string]$Code)
    $normalizedCode = ($Code -replace "[\r\n]+", " ").Trim()
    $all = @("--yes", "--package", "@playwright/cli", "playwright-cli", "--session", $session, "run-code", $normalizedCode)
    & $npx @all
    if ($LASTEXITCODE -ne 0) {
        throw "playwright-cli run-code failed."
    }
}

function Js-Literal {
    param([AllowEmptyString()][string]$Value)
    $escaped = $Value.Replace('\', '\\').Replace("'", "\'")
    return "'$escaped'"
}

function Js-Array {
    param([AllowEmptyString()][string[]]$Values)
    return "[" + (($Values | ForEach-Object { Js-Literal $_ }) -join ",") + "]"
}

function Credential-Value {
    param([string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($value) { return $value }
    return $Default
}

function Save-PageArtifacts {
    param([string]$Role, [string]$Path)
    $slug = (($Role + "-" + $Path) -replace "[^A-Za-z0-9]+", "-").Trim("-").ToLowerInvariant()
    $snapshotPath = Join-Path $artifactRoot "snapshot-$slug.txt"
    Invoke-Pw snapshot | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
    Invoke-Pw screenshot
}

function Invoke-RoleLogin {
    param([string]$Username, [string]$Password)
    $loginUrl = Js-Literal "$base/accounts/login/"
    $usernameValue = Js-Literal $Username
    $passwordValue = Js-Literal $Password
    Invoke-Pw open "$base/accounts/login/"
    Invoke-PwCode @"
async page => {
const response = await page.goto($loginUrl);
if (!response || response.status() >= 400) throw new Error('Login page did not load');
await page.getByLabel('Username').fill($usernameValue);
await page.getByLabel('Password').fill($passwordValue);
await page.getByRole('button', {name: /Sign in/}).click();
await page.waitForLoadState('domcontentloaded');
if ((await page.locator('body').innerText()).includes('Invalid username or password')) throw new Error('Simulation credentials were rejected');
}
"@
}

function Invoke-OwnerLogin {
    param([string]$Username, [string]$Password)
    $gateIdentifier = Js-Literal $Username
    $usernameValue = Js-Literal $Username
    $passwordValue = Js-Literal $Password
    Invoke-Pw open "$base/gccad/"
    Invoke-PwCode @"
async page => {
const body = await page.locator('body').innerText();
if (body.includes('Before we begin')) {
await page.getByLabel('Admin username or email').fill($gateIdentifier);
await page.getByRole('button', {name: /Continue to sign in/}).click();
await page.waitForLoadState('domcontentloaded');
}
await page.getByLabel('Username').fill($usernameValue);
await page.getByLabel('Password').fill($passwordValue);
await page.getByRole('button', {name: /Log in/}).click();
await page.waitForLoadState('domcontentloaded');
const currentBody = await page.locator('body').innerText();
if (currentBody.includes('Invalid username or password')) throw new Error('Owner administrator credentials were rejected');
}
"@
}

function Assert-Page {
    param(
        [string]$Role,
        [string]$Path,
        [int[]]$ExpectedStatus = @(200),
        [string[]]$MustContain = @(),
        [string[]]$MustNotContain = @(),
        [string]$Description = ""
    )
    $url = Js-Literal "$base$Path"
    $expected = "[" + ($ExpectedStatus -join ",") + "]"
    $required = Js-Array $MustContain
    $forbidden = Js-Array $MustNotContain
    try {
        Invoke-PwCode @"
async page => {
const response = await page.goto($url);
const status = response ? response.status() : 0;
const expected = $expected;
if (!expected.includes(status)) throw new Error('Expected HTTP ' + expected.join(',') + ' but received ' + status);
const body = await page.locator('body').innerText();
for (const item of $required) if (!body.includes(item)) throw new Error('Missing expected text: ' + item);
for (const item of $forbidden) if (body.includes(item)) throw new Error('Found forbidden text: ' + item);
}
"@
        Save-PageArtifacts $Role $Path
        Add-Check $Role $Path $Description
    } catch {
        Fail-Check $Role $Path $Description $_.Exception.Message
    }
}

function Run-Role {
    param(
        [string]$Role,
        [string]$Username,
        [string]$Password,
        [object[]]$Assertions
    )
    try {
        try { Invoke-Pw close } catch {}
        if ($Role -eq "owner") {
            Invoke-OwnerLogin $Username $Password
        } else {
            Invoke-RoleLogin $Username $Password
        }
        foreach ($assertion in $Assertions) {
            Assert-Page -Role $Role -Path $assertion.Path -ExpectedStatus $assertion.Status -MustContain $assertion.Contains -MustNotContain $assertion.Excludes -Description $assertion.Description
        }
    } catch {
        if (-not ($failures | Where-Object { $_.role -eq $Role })) {
            $failures.Add([ordered]@{
                role = $Role
                path = ""
                description = "Role smoke flow"
                passed = $false
                error = $_.Exception.Message
            })
        }
    }
}

$ownerUsername = Credential-Value "GCC_SMOKE_OWNER_USERNAME" "sim-owner"
$ownerPassword = Credential-Value "GCC_SMOKE_OWNER_PASSWORD" "SimOnly-Owner-2026!"
$officeUsername = Credential-Value "GCC_SMOKE_OFFICE_USERNAME" "sim-office"
$officePassword = Credential-Value "GCC_SMOKE_OFFICE_PASSWORD" "SimOnly-Office-2026!"
$managerUsername = Credential-Value "GCC_SMOKE_MANAGER_USERNAME" "sim-manager"
$managerPassword = Credential-Value "GCC_SMOKE_MANAGER_PASSWORD" "SimOnly-Manager-2026!"
$salesUsername = Credential-Value "GCC_SMOKE_SALES_USERNAME" "sim-sales"
$salesPassword = Credential-Value "GCC_SMOKE_SALES_PASSWORD" "SimOnly-Sales-2026!"
$fieldUsername = Credential-Value "GCC_SMOKE_FIELD_USERNAME" "sim-field"
$fieldPassword = Credential-Value "GCC_SMOKE_FIELD_PASSWORD" "SimOnly-Field-2026!"
$subcontractorUsername = Credential-Value "GCC_SMOKE_SUBCONTRACTOR_USERNAME" "sim-subcontractor"
$subcontractorPassword = Credential-Value "GCC_SMOKE_SUBCONTRACTOR_PASSWORD" "SimOnly-Subcontractor-2026!"
$clientUsername = Credential-Value "GCC_SMOKE_CLIENT_USERNAME" "sim-client"
$clientPassword = Credential-Value "GCC_SMOKE_CLIENT_PASSWORD" "SimOnly-Client-2026!"
$unauthorizedUsername = Credential-Value "GCC_SMOKE_UNAUTHORIZED_USERNAME" "sim-unauthorized"
$unauthorizedPassword = Credential-Value "GCC_SMOKE_UNAUTHORIZED_PASSWORD" "SimOnly-Unauthorized-2026!"

try {
    Push-Location $artifactRoot
    $openArguments = @("open", "$base/accounts/login/")
    if ($Headed) { $openArguments += "--headed" }
    Invoke-Pw @openArguments
    Save-PageArtifacts "anonymous" "/accounts/login/"

    Run-Role "owner" $ownerUsername $ownerPassword @(
        [pscustomobject]@{ Path = "/dashboard/"; Status = @(200); Contains = @("Overview", "Projects"); Excludes = @("What needs my attention today?", "Required approvals complete"); Description = "Owner standard Overview" },
        [pscustomobject]@{ Path = $projectPath; Status = @(200); Contains = @("Project record", "Project sequence"); Excludes = @("Ready for construction", "Client decisions", "Field requests"); Description = "Owner standard project record" },
        [pscustomobject]@{ Path = "/dashboard/reviews/weekly/"; Status = @(200); Contains = @("Overview"); Excludes = @("Weekly action list"); Description = "Legacy weekly-review redirect" },
        [pscustomobject]@{ Path = "/dashboard/calendar/"; Status = @(200); Contains = @("Calendar"); Excludes = @(); Description = "Owner calendar" }
    )

    Run-Role "office" $officeUsername $officePassword @(
        [pscustomobject]@{ Path = "/team/"; Status = @(200); Contains = @("Your work", "Assigned projects"); Excludes = @("Financial ledger", "Gross margin"); Description = "Office assigned-work workspace" },
        [pscustomobject]@{ Path = $projectPath; Status = @(200); Contains = @("Projects"); Excludes = @("Financial ledger", "Gross margin", "Ready for construction"); Description = "Office standard project workspace" }
    )

    Run-Role "manager" $managerUsername $managerPassword @(
        [pscustomobject]@{ Path = $projectPath; Status = @(200); Contains = @("Projects"); Excludes = @("Financial ledger", "Ready for construction"); Description = "Manager standard project workspace" }
    )

    Run-Role "sales" $salesUsername $salesPassword @(
        [pscustomobject]@{ Path = "/team/"; Status = @(200); Contains = @("Your work"); Excludes = @("Financial ledger", "Gross margin"); Description = "Sales team workspace" },
        [pscustomobject]@{ Path = "/dashboard/leads/"; Status = @(200); Contains = @("Leads"); Excludes = @("Financial ledger", "Gross margin"); Description = "Sales lead access" },
        [pscustomobject]@{ Path = "/dashboard/estimates/"; Status = @(200); Contains = @("Estimates"); Excludes = @("Financial ledger", "Gross margin"); Description = "Sales estimate access" }
    )

    Run-Role "field" $fieldUsername $fieldPassword @(
        [pscustomobject]@{ Path = "/team/field/"; Status = @(200); Contains = @("Where am I going? What am I doing? What do I need?"); Excludes = @("Financial ledger", "Gross margin"); Description = "Field today workspace" },
        [pscustomobject]@{ Path = $projectPath; Status = @(200); Contains = @("Assigned projects"); Excludes = @("Financial ledger", "Gross margin", "Daily reports, materials, and problems"); Description = "Field assigned-project workspace" }
    )

    Run-Role "client" $clientUsername $clientPassword @(
        [pscustomobject]@{ Path = "/portal/"; Status = @(200); Contains = @("YOUR PROJECT", "PROJECT DECISIONS"); Excludes = @("Financial ledger", "Gross margin", "Internal notes"); Description = "Client project portal" }
    )

    Run-Role "subcontractor" $subcontractorUsername $subcontractorPassword @(
        [pscustomobject]@{ Path = $projectPath; Status = @(403, 404); Contains = @(); Excludes = @("Financial ledger", "Gross margin"); Description = "Subcontractor project boundary" }
    )

    Run-Role "unauthorized" $unauthorizedUsername $unauthorizedPassword @(
        [pscustomobject]@{ Path = $projectPath; Status = @(403, 404); Contains = @(); Excludes = @("Financial ledger", "Gross margin"); Description = "Unassigned user project boundary" },
        [pscustomobject]@{ Path = "/api/v1/projects/$ProjectId/financials/"; Status = @(403, 404); Contains = @(); Excludes = @("gross_profit", "gross_margin"); Description = "Unauthorized financial API boundary" }
    )

    try { Invoke-Pw close } catch {}
    $report = [ordered]@{
        scenario = "browser-smoke"
        passed = ($failures.Count -eq 0)
        base_url = $base
        project_id = $ProjectId
        checks = @($checks)
        failures = @($failures)
        artifact_directory = $artifactRoot
    }
    $report.artifact_files = @(Get-ChildItem -LiteralPath $artifactRoot -File | ForEach-Object { $_.FullName })
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $resolvedReportPath -Encoding UTF8
    if ($failures.Count -gt 0) { throw "Browser smoke failed with $($failures.Count) failure(s)." }
    Write-Host "Grand Coast browser smoke passed. Checks: $($checks.Count)"
}
catch {
    try { Invoke-Pw close } catch {}
    $failureReport = [ordered]@{
        scenario = "browser-smoke"
        passed = $false
        base_url = $base
        project_id = $ProjectId
        checks = @($checks)
        failures = @($failures)
        error = $_.Exception.Message
        artifact_directory = $artifactRoot
    }
    $failureReport.artifact_files = @(Get-ChildItem -LiteralPath $artifactRoot -File | ForEach-Object { $_.FullName })
    $failureReport | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $resolvedReportPath -Encoding UTF8
    throw
}
finally {
    Pop-Location
}
