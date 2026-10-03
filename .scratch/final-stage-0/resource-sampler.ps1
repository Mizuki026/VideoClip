param(
    [ValidateRange(1, 60)]
    [int]$IntervalSeconds = 10,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [Parameter(Mandatory = $true)]
    [string]$StopFile
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$allowedOutputRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd(
    [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
) + [IO.Path]::DirectorySeparatorChar
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
if (-not $resolvedOutput.StartsWith($allowedOutputRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resource sample output must remain under .scratch/final-stage-0.'
}

$environmentFile = Join-Path $repoRoot 'data\mvp-stage-2\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
$appRoot = Join-Path $repoRoot 'data\mvp-stage-2\app'
if (-not (Test-Path -LiteralPath $environmentFile)) { throw 'Stage 2 environment file is missing.' }
if (Test-Path -LiteralPath $resolvedOutput) { throw 'Refusing to overwrite an existing resource sample.' }
if (Test-Path -LiteralPath $StopFile) { throw 'Stop file already exists; use a unique stop-file path.' }

while (-not (Test-Path -LiteralPath $StopFile)) {
    $files = @(Get-ChildItem -LiteralPath $appRoot -Force -Recurse -File -ErrorAction SilentlyContinue)
    $freeMemoryKiB = (Get-CimInstance -ClassName Win32_OperatingSystem -Property FreePhysicalMemory).FreePhysicalMemory
    $containerMemory = @{
        api = ''
        worker = ''
        postgres = ''
        redis = ''
    }
    $stats = & docker compose --env-file $environmentFile -p videoclip-mvp-stage2 -f $composeFile stats --no-stream --format '{{.Name}}|{{.MemUsage}}' 2>$null
    if ($LASTEXITCODE -eq 0) {
        foreach ($line in $stats) {
            $parts = $line -split '\|', 2
            if ($parts.Count -ne 2) { continue }
            foreach ($service in $containerMemory.Keys) {
                if ($parts[0] -match "videoclip-mvp-stage2-$service-\d+$") {
                    $containerMemory[$service] = $parts[1]
                    break
                }
            }
        }
    }

    [PSCustomObject]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        e_free_bytes = [Int64](Get-PSDrive -Name E).Free
        host_available_memory_bytes = [Int64]$freeMemoryKiB * 1024
        stage2_app_file_count = $files.Count
        stage2_app_bytes = [Int64](($files | Measure-Object -Property Length -Sum).Sum)
        api_memory = $containerMemory.api
        worker_memory = $containerMemory.worker
        postgres_memory = $containerMemory.postgres
        redis_memory = $containerMemory.redis
    } | Export-Csv -LiteralPath $resolvedOutput -NoTypeInformation -Append -Encoding utf8

    Start-Sleep -Seconds $IntervalSeconds
}
