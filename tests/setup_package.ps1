param([string]$SetupPath = (Join-Path $PSScriptRoot '../setup.ps1'))
$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath $SetupPath -Raw
$taskSetupAst = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$null, [ref]$null)
$taskNodeStatements = $taskSetupAst.EndBlock.Statements | Where-Object { $_.Extent.Text -match '\bnpm\.cmd\b' }
$taskSetupRoot = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$taskOldLocation = Get-Location
$script:taskNpmCalls = @()
function npm.cmd { $script:taskNpmCalls += ($args -join ' '); $global:LASTEXITCODE = 0 }
try {
    New-Item -ItemType Directory -Path $taskSetupRoot | Out-Null
    Set-Location $taskSetupRoot
    # Run the setup's Node/build section with an extracted package's file layout.
    foreach ($statement in $taskNodeStatements) { Invoke-Expression $statement.Extent.Text }
    if ($script:taskNpmCalls.Count -ne 0) { throw 'Packaged setup attempted a source build or Node install.' }
    Set-Content -LiteralPath 'tsconfig.json' -Value '{}'
    foreach ($statement in $taskNodeStatements) { Invoke-Expression $statement.Extent.Text }
    if (($script:taskNpmCalls -join ',') -ne 'ci,run build') { throw 'Source setup skipped its Node install/build.' }
    Write-Output 'Setup checks passed: packaged and source checkout paths'
} finally {
    Set-Location $taskOldLocation
    if ([System.IO.Path]::GetFullPath($taskSetupRoot).StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $taskSetupRoot -Recurse -Force
    }
}
