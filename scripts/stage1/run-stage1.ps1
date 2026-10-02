$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$environmentFile = Join-Path $repoRoot 'data\mvp-stage-1\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw 'Run prepare-private-data.ps1 first.'
}

Push-Location $repoRoot
try {
    & docker compose --env-file $environmentFile -f $composeFile build api
    if ($LASTEXITCODE -ne 0) { throw "API image build failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -f $composeFile up -d postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL startup failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -f $composeFile run --rm api alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -f $composeFile up -d api
    if ($LASTEXITCODE -ne 0) { throw "API startup failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -f $composeFile ps
}
finally {
    Pop-Location
}
