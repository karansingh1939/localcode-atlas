$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
}

& $python -m pip install -r requirements-windows.lock
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& $python -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Backend installation failed.' }

$taskConfig = (& $python -c 'import json; from backend.config import Settings; print(json.dumps(dict(host=Settings.OLLAMA_HOST, model=Settings.EMBED_MODEL)))') | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not load backend configuration.' }
$env:OLLAMA_HOST = $taskConfig.host

try {
    Invoke-RestMethod "$($taskConfig.host)/api/tags" -TimeoutSec 3 | Out-Null
} catch {
    Start-Process -FilePath (Get-Command ollama -ErrorAction Stop).Source -ArgumentList 'serve' -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod "$($taskConfig.host)/api/tags" -TimeoutSec 1 | Out-Null
            $ready = $true
            break
        } catch {}
    }
    if (-not $ready) { throw "Ollama did not start at $($taskConfig.host)." }
}
ollama list | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The Ollama CLI is not working.' }
$taskModelNames = (Invoke-RestMethod "$($taskConfig.host)/api/tags" -TimeoutSec 3).models.name
$taskModelName = if ($taskConfig.model.Contains(':')) { $taskConfig.model } else { "$($taskConfig.model):latest" }
if ($taskModelNames -notcontains $taskModelName) {
    ollama pull $taskConfig.model
    if ($LASTEXITCODE -ne 0) { throw 'Ollama model installation failed.' }
}

if (Test-Path -LiteralPath 'tsconfig.json') {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Extension build failed.' }
}
