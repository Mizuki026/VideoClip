param(
    [Parameter(Mandatory = $true)]
    [string]$AttemptId
)

$ErrorActionPreference = 'Stop'
$parsedAttemptId = [Guid]::Empty
if (-not [Guid]::TryParse($AttemptId, [ref]$parsedAttemptId)) {
    throw 'AttemptId must be a valid UUID returned by this project task status endpoint.'
}
$attemptValue = $parsedAttemptId.ToString('D')
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
    $lookup = @"
SELECT t.id::text || '|' || a.work_directory_name || '|' || a.cleanup_state
FROM analysis_attempts a
JOIN analysis_tasks t ON t.id = a.task_id
JOIN analysis_executions e ON e.id = a.execution_id
WHERE a.id = '$attemptValue'
  AND a.status = 'failed'
  AND a.stop_confirmed_at IS NOT NULL
  AND e.stop_confirmed_at IS NOT NULL
"@
    $row = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $lookup).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $row) {
        throw 'Attempt is not a failed Stage 2 attempt with a confirmed stopped execution.'
    }
    $parts = $row -split '\|', 3
    if ($parts.Count -ne 3 -or $parts[0] -notmatch '^[0-9a-fA-F-]{36}$' -or $parts[1] -notmatch '^[0-9a-f]{32}$') {
        throw 'Database returned an invalid controlled attempt path.'
    }
    $taskDirectoryName = ([Guid]$parts[0]).ToString('N')
    $workDirectoryName = $parts[1]
    $analysisRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'data\mvp-stage-2\app\analysis'))
    $taskDirectory = [IO.Path]::GetFullPath((Join-Path $analysisRoot $taskDirectoryName))
    $attemptDirectory = [IO.Path]::GetFullPath((Join-Path $taskDirectory $workDirectoryName))
    $requiredPrefix = $analysisRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $attemptDirectory.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetDirectoryName($attemptDirectory) -ne $taskDirectory) {
        throw 'Resolved attempt path is outside the Stage 2 analysis directory.'
    }

    if ($parts[2] -ne 'cleaned' -and (Test-Path -LiteralPath $attemptDirectory)) {
        foreach ($candidatePath in @($analysisRoot, $taskDirectory, $attemptDirectory)) {
            if (Test-Path -LiteralPath $candidatePath) {
                $candidate = Get-Item -LiteralPath $candidatePath -Force
                if (($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw 'A controlled directory is a reparse point; no files were removed.'
                }
            }
        }
        $entries = @(Get-ChildItem -LiteralPath $attemptDirectory -Force -Recurse)
        foreach ($entry in $entries) {
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'An attempt entry is a reparse point; no files were removed.'
            }
        }
        Remove-Item -LiteralPath $attemptDirectory -Recurse -Force
    }
    if (Test-Path -LiteralPath $attemptDirectory) {
        throw 'Attempt files remain after cleanup; database cleanup state was left unchanged.'
    }

    $markCleaned = @"
UPDATE analysis_attempts
SET cleanup_state = 'cleaned'
WHERE id = '$attemptValue'
  AND status = 'failed'
  AND stop_confirmed_at IS NOT NULL
  AND cleanup_state <> 'cleaned'
RETURNING id
"@
    $updated = (& docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $databaseUser -d $databaseName -c $markCleaned).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not record the confirmed attempt cleanup state.' }
    if ($updated -and $updated -ne $attemptValue) { throw 'Unexpected attempt was marked cleaned.' }
    Write-Output "Attempt $attemptValue has a confirmed stopped execution and cleanup_state=cleaned. The source video and transcript records were preserved."
}
finally {
    Pop-Location
}
