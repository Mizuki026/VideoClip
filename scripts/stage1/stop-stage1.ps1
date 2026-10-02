$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$environmentFile = Join-Path $repoRoot 'data\mvp-stage-1\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw 'Stage 1 private configuration was not found.'
}

Push-Location $repoRoot
try {
    & docker compose --env-file $environmentFile -f $composeFile stop api postgres
    if ($LASTEXITCODE -ne 0) { throw "Stage 1 stop failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}
