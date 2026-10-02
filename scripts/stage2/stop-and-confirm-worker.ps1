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
    $precheck = "SELECT count(*) FROM analysis_executions WHERE id = '$executionValue' AND stop_confirmed_at IS NULL"
    $matching = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $precheck).Trim()
    if ($LASTEXITCODE -ne 0 -or $matching -ne '1') {
        throw 'The specified execution is not an unconfirmed Stage 2 execution. No stop was performed.'
    }

    $workerIds = @(& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile ps --all -q worker)
    if ($LASTEXITCODE -ne 0 -or $workerIds.Count -gt 1) {
        throw 'Cannot establish a single Stage 2 Worker container; no database stop confirmation was written.'
    }
    if ($workerIds.Count -eq 1) {
        $workerId = $workerIds[0].Trim()
        $running = (& docker inspect --format '{{.State.Running}}' $workerId).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Stage 2 Worker container.' }
        if ($running -eq 'true') {
            & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile stop worker
            if ($LASTEXITCODE -ne 0) { throw 'Worker stop did not complete; database state was left unchanged.' }
        }
        $running = (& docker inspect --format '{{.State.Running}}' $workerId).Trim()
        $state = (& docker inspect --format '{{.State.Status}}' $workerId).Trim()
        if ($LASTEXITCODE -ne 0 -or $running -ne 'false' -or $state -ne 'exited') {
            throw 'Docker did not confirm the Stage 2 Worker container and its child processes stopped.'
        }
    }

    $confirmation = @"
WITH stopped AS MATERIALIZED (
  UPDATE analysis_executions
  SET stop_confirmed_at = now(), stop_confirmation_source = 'operator_container_exit',
      stop_reason = 'operator_confirmed_container_stopped'
  WHERE id = '$executionValue' AND stop_confirmed_at IS NULL
  RETURNING task_id, attempt_id
), changed_attempt AS (
  UPDATE analysis_attempts a
  SET stop_confirmed_at = now(), finished_at = COALESCE(a.finished_at, now()),
      status = CASE WHEN a.status = 'running' THEN 'failed' ELSE a.status END,
      failure_code = CASE WHEN a.status = 'running' THEN COALESCE(a.failure_code, 'WORKER_STOPPED_OPERATOR') ELSE a.failure_code END
  FROM stopped s WHERE a.id = s.attempt_id RETURNING a.id
), changed_task AS (
  UPDATE analysis_tasks t
  SET status = CASE WHEN t.status = 'running' THEN 'failed' ELSE t.status END,
      stage = CASE WHEN t.status = 'running' THEN 'failed' ELSE t.stage END,
      failure_code = CASE WHEN t.status = 'running' THEN COALESCE(t.failure_code, 'WORKER_STOPPED_OPERATOR') ELSE t.failure_code END,
      updated_at = now()
  FROM stopped s WHERE t.id = s.task_id RETURNING t.id
)
SELECT count(*) FROM stopped
"@
    $confirmed = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $confirmation).Trim()
    if ($LASTEXITCODE -ne 0 -or $confirmed -ne '1') {
        throw 'Stop confirmation was not written. Keep the project database available and inspect the execution record.'
    }
    Write-Output "Execution $executionValue is confirmed stopped; an unfinished attempt was recorded as failed. No retry was created."
}
finally {
    Pop-Location
}
