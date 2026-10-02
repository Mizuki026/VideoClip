$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$environmentFile = Join-Path $repoRoot 'data\mvp-stage-2\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
if (-not (Test-Path -LiteralPath $environmentFile)) { throw 'Stage 2 private environment file is missing.' }

Push-Location $repoRoot
try {
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile stop worker
    if ($LASTEXITCODE -ne 0) { throw 'Stage 2 Worker stop failed; database and Redis were left running.' }

    $databaseName = ((Get-Content -LiteralPath $environmentFile | Where-Object { $_ -match '^POSTGRES_DB=' } | Select-Object -First 1) -split '=', 2)[1]
    $databaseUser = ((Get-Content -LiteralPath $environmentFile | Where-Object { $_ -match '^POSTGRES_USER=' } | Select-Object -First 1) -split '=', 2)[1]
    if ($databaseName -notmatch '^[A-Za-z0-9_]+$' -or $databaseUser -notmatch '^[A-Za-z0-9_]+$') {
        throw 'Stage 2 database identifiers contain unsupported characters.'
    }
    $unconfirmedSql = 'SELECT id FROM analysis_executions WHERE stop_confirmed_at IS NULL ORDER BY started_at'
    $unconfirmed = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $unconfirmedSql)
    if ($LASTEXITCODE -ne 0) { throw 'Could not check worker execution records; Stage 2 database remains running.' }
    if ($unconfirmed.Count -gt 0) {
        $ids = $unconfirmed -join ', '
        throw "An execution still requires explicit stop confirmation: $ids. Run scripts/stage2/stop-and-confirm-worker.ps1 for each execution before stopping PostgreSQL."
    }

    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile stop api redis postgres
    if ($LASTEXITCODE -ne 0) { throw 'Stage 2 service stop failed.' }
}
finally {
    Pop-Location
}
