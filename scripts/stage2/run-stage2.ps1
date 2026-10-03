$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$environmentFile = Join-Path $repoRoot 'data\mvp-stage-2\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw 'Run prepare-stage2.ps1 first.'
}

Push-Location $repoRoot
try {
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile build api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw "Stage 2 image build failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile up -d postgres redis
    if ($LASTEXITCODE -ne 0) { throw "Stage 2 database and Redis startup failed: $LASTEXITCODE" }

    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    do {
        $postgresId = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q postgres).Trim()
        $redisId = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q redis).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $postgresId -or -not $redisId) { throw 'Stage 2 database or Redis container did not start.' }
        $postgresHealth = (& docker inspect --format '{{.State.Health.Status}}' $postgresId).Trim()
        $redisHealth = (& docker inspect --format '{{.State.Health.Status}}' $redisId).Trim()
        if ($postgresHealth -eq 'healthy' -and $redisHealth -eq 'healthy') { break }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Timed out waiting for Stage 2 PostgreSQL and Redis health checks.' }
        Start-Sleep -Seconds 2
    } while ($true)

    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile run --rm api alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Stage 2 database migration failed: $LASTEXITCODE" }
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile up -d api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw "Stage 2 API or Worker startup failed: $LASTEXITCODE" }

    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:58002/health' -TimeoutSec 3
            if ($health.status -eq 'ok') { break }
        } catch { }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Stage 2 API did not become healthy.' }
        Start-Sleep -Seconds 2
    } while ($true)

    $workerIds = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q worker)
    if ($LASTEXITCODE -ne 0 -or $workerIds.Count -ne 1) { throw 'Stage 2 must run exactly one Worker container.' }
    $maintenanceIds = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q maintenance-worker)
    if ($LASTEXITCODE -ne 0 -or $maintenanceIds.Count -ne 1) { throw 'Stage 2 must run exactly one maintenance Worker container.' }
    $beatIds = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q beat)
    if ($LASTEXITCODE -ne 0 -or $beatIds.Count -ne 1) { throw 'Stage 2 must run exactly one Celery Beat container.' }
    $analysisCommand = (& docker inspect --format '{{json .Config.Cmd}}' $workerIds[0]).Trim()
    $maintenanceCommand = (& docker inspect --format '{{json .Config.Cmd}}' $maintenanceIds[0]).Trim()
    if ($analysisCommand -notmatch '"--queues=analysis"' -or $analysisCommand -notmatch '"--concurrency=1"') {
        throw 'The analysis Worker must consume only the analysis queue with concurrency one.'
    }
    if ($maintenanceCommand -notmatch '"--queues=maintenance"' -or $maintenanceCommand -notmatch '"--concurrency=1"') {
        throw 'The maintenance Worker must consume only the maintenance queue with concurrency one.'
    }
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps
}
finally {
    Pop-Location
}
