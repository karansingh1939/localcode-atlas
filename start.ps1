$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run npm run setup first.'
}
$taskConfig = (& $python -c 'from backend.config import Settings; print(Settings.OLLAMA_HOST)').Trim()
$env:OLLAMA_HOST = $taskConfig
try {
    Invoke-RestMethod "$taskConfig/api/tags" -TimeoutSec 3 | Out-Null
} catch {
    Start-Process -FilePath (Get-Command ollama -ErrorAction Stop).Source -ArgumentList 'serve' -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod "$taskConfig/api/tags" -TimeoutSec 1 | Out-Null
            $ready = $true
            break
        } catch {}
    }
    if (-not $ready) { throw "Ollama did not start at $taskConfig." }
}
try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8000/status' -TimeoutSec 3
} catch { $health = $null }
if ($null -ne $health -and $null -ne $health.indexed_chunks -and $null -ne $health.embed_model) {
    Write-Host 'LocalCode Atlas is already running at http://127.0.0.1:8000'
    exit 0
}
& $python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
exit $LASTEXITCODE
