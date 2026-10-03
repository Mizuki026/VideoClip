param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutionId
)

$ErrorActionPreference = 'Stop'
$parsedExecutionId = [Guid]::Empty
if (-not [Guid]::TryParse($ExecutionId, [ref]$parsedExecutionId)) {
    throw 'ExecutionId must be a valid UUID returned by this project task status endpoint.'
}
$executionValue = $parsedExecutionId.ToString('D')
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$environmentFile = Join-Path $repoRoot 'data\mvp-stage-2\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
if (-not (Test-Path -LiteralPath $environmentFile)) { throw 'Stage 2 private environment file is missing.' }

function Get-DotEnvValue([string]$Key) {
    $prefix = [regex]::Escape("$Key=")
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        if ($line -match "^$prefix(.*)$") { return $Matches[1].Trim() }
    }
    throw "Required Stage 2 setting is missing: $Key"
}

$databaseName = Get-DotEnvValue 'POSTGRES_DB'
$databaseUser = Get-DotEnvValue 'POSTGRES_USER'
if ($databaseName -notmatch '^[A-Za-z0-9_]+$' -or $databaseUser -notmatch '^[A-Za-z0-9_]+$') {
    throw 'Stage 2 database identifiers contain unsupported characters.'
}

Push-Location $repoRoot
try {
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile up -d postgres
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the Stage 2 database for stop confirmation.' }
    $databaseDeadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        $postgresId = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps -q postgres).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $postgresId) { throw 'Stage 2 PostgreSQL is unavailable.' }
        $postgresHealth = (& docker inspect --format '{{.State.Health.Status}}' $postgresId).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Stage 2 PostgreSQL health.' }
        if ($postgresHealth -eq 'healthy') { break }
        if ([DateTime]::UtcNow -ge $databaseDeadline) { throw 'Stage 2 PostgreSQL did not become healthy.' }
        Start-Sleep -Seconds 2
    } while ($true)

    $lookup = @"
SELECT coalesce(worker_instance_id::text, '<null>') || '|' || coalesce(worker_container_id, '<null>') || '|' ||
       coalesce(process_scope, '<null>') || '|' || coalesce(boundary_pid::text, '<null>') || '|' ||
       coalesce(boundary_start_ticks::text, '<null>') || '|' || coalesce(boundary_session_id::text, '<null>') || '|' ||
       coalesce(boundary_boot_id, '<null>')
FROM analysis_executions
WHERE id = '$executionValue' AND stop_confirmed_at IS NULL
"@
    $binding = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $lookup).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $binding) {
        throw 'The specified execution is not an unconfirmed Stage 2 execution. No stop was performed.'
    }
    $parts = $binding -split '\|', 7
    if ($parts.Count -ne 7 -or $parts -contains '<null>' -or $parts[2] -ne 'linux-session-v1') {
        throw 'This execution has no supported supervised identity binding. It remains blocked for manual handling.'
    }
    $workerInstanceId = [Guid]::Empty
    if (-not [Guid]::TryParse($parts[0], [ref]$workerInstanceId)) {
        throw 'The execution Worker generation is invalid; no stop was performed.'
    }

    $workerIds = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps --all -q worker)
    if ($LASTEXITCODE -ne 0 -or $workerIds.Count -ne 1) {
        throw 'Cannot establish exactly one Stage 2 Worker container; no database stop confirmation was written.'
    }
    $workerId = $workerIds[0].Trim()
    $containerInstanceSetting = @(& docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $workerId | Where-Object { $_ -match '^ANALYSIS_WORKER_INSTANCE_ID=' })
    if ($LASTEXITCODE -ne 0 -or $containerInstanceSetting.Count -ne 1) {
        throw 'The Stage 2 Worker container has no inspectable instance generation.'
    }
    $containerInstanceId = [Guid]::Empty
    if (-not [Guid]::TryParse(($containerInstanceSetting[0] -split '=', 2)[1], [ref]$containerInstanceId) -or
        $containerInstanceId -ne $workerInstanceId) {
        throw 'The target execution does not belong to this Worker instance; no stop was performed.'
    }
    $shortContainerId = (& docker inspect --format '{{.Id}}' $workerId).Trim()
    $boundContainerId = $parts[1]
    if ($LASTEXITCODE -ne 0 -or $shortContainerId.Length -lt 12 -or
        $boundContainerId -ne $shortContainerId.Substring(0, 12)) {
        throw 'The target execution does not match this exact Worker container; no stop was performed.'
    }

    $running = (& docker inspect --format '{{.State.Running}}' $workerId).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Stage 2 Worker container.' }
    if ($running -eq 'true') {
        & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile stop --timeout 0 worker
        if ($LASTEXITCODE -ne 0) { throw 'Worker stop did not complete; database state was left unchanged.' }
    }
    $finalContainerId = (& docker inspect --format '{{.Id}}' $workerId).Trim()
    $running = (& docker inspect --format '{{.State.Running}}' $workerId).Trim()
    $state = (& docker inspect --format '{{.State.Status}}' $workerId).Trim()
    if ($LASTEXITCODE -ne 0 -or $finalContainerId -ne $shortContainerId -or $running -ne 'false' -or $state -ne 'exited') {
        throw 'Docker did not confirm that the exact bound Stage 2 Worker container exited.'
    }

    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile up -d api
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the Stage 2 API for stop confirmation.' }
    $apiDeadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:58002/health' -TimeoutSec 3
            if ($health.status -eq 'ok') { break }
        } catch { }
        if ([DateTime]::UtcNow -ge $apiDeadline) { throw 'Stage 2 API did not become healthy for stop confirmation.' }
        Start-Sleep -Seconds 2
    } while ($true)

    $workerInstanceIdValue = $workerInstanceId.ToString('D')
    & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T api python -m videoclip.stop_confirmation `
        --execution-id $executionValue `
        --worker-instance-id $workerInstanceIdValue `
        --container-id $finalContainerId `
        --container-exited
    if ($LASTEXITCODE -ne 0) {
        throw 'The stopped container evidence did not match the current execution binding. Keep the database and Worker stopped for manual review.'
    }
    $confirmedCount = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c "SELECT count(*) FROM analysis_executions WHERE id = '$executionValue' AND stop_confirmed_at IS NOT NULL").Trim()
    if ($LASTEXITCODE -ne 0 -or $confirmedCount -ne '1') {
        throw 'The exact execution stop confirmation was not persisted.'
    }
    Write-Output "Execution $executionValue is confirmed stopped after container $finalContainerId exited. No retry was created."
}
finally {
    Pop-Location
}
