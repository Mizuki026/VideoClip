param(
    [ValidateRange(5, 120)]
    [int]$IntervalSeconds = 30,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [Parameter(Mandatory = $true)]
    [string]$StopFile
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$allowedStopRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
$allowedDataRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'data\mvp-stage-2\acceptance')).TrimEnd('\') + '\'
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$resolvedStop = [IO.Path]::GetFullPath($StopFile)
$resolvedRunRoot = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedOutput))
$outputIsAllowed = $resolvedRunRoot.StartsWith($allowedDataRoot, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $resolvedRunRoot) -match '^final-stage7-' -and (Split-Path -Leaf $resolvedOutput) -eq 'resource-samples.csv'
if (-not $outputIsAllowed) {
    throw 'Resource CSV must be resource-samples.csv in a private final-stage7 run directory.'
}
if (-not $resolvedStop.StartsWith($allowedStopRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resource stop marker must remain under .scratch/final-stage-7.'
}
if (Test-Path -LiteralPath $resolvedOutput) { throw 'Refusing to overwrite a resource sample.' }
if (Test-Path -LiteralPath $resolvedStop) { throw 'Refusing to reuse a resource stop marker.' }

$environmentFile = Join-Path $repoRoot 'data\mvp-stage-2\.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
$businessRoot = Join-Path $repoRoot 'data\mvp-stage-2'
$appRoot = Join-Path $businessRoot 'app'
$compose = @('compose', '--env-file', $environmentFile, '-p', 'videoclip-mvp-stage2', '-f', $composeFile)
$services = @('api', 'worker', 'maintenance-worker', 'beat', 'postgres', 'redis')

function Measure-DirectoryBytes([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return [Int64]0 }
    try {
        $files = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -File -ErrorAction Stop)
        $total = ($files | Measure-Object -Property Length -Sum).Sum
        return [Int64]($total ?? 0)
    } catch {
        return [Int64]-1
    }
}

function Get-ContainerMetric([string]$Name, [string]$Metrics, [int]$FieldIndex) {
    foreach ($line in ($Metrics -split "`r?`n")) {
        $parts = $line -split '\|', 4
        if ($parts.Count -ge 2 -and $parts[0].TrimStart('/') -match "videoclip-mvp-stage2-$([regex]::Escape($Name))-\d+$") {
            return $parts[$FieldIndex]
        }
    }
    return ''
}

while (-not (Test-Path -LiteralPath $resolvedStop)) {
    $fileSet = @(& docker @compose ps -q $services)
    if ($LASTEXITCODE -ne 0) { $fileSet = @() }
    $containerIds = @($fileSet | Where-Object { $_ -match '^[0-9a-f]{12,64}$' })

    $inspect = ''
    if ($containerIds.Count -gt 0) {
        $inspect = @(& docker inspect --size --format '{{.Name}}|{{.SizeRw}}|{{.SizeRootFs}}|{{.LogPath}}' $containerIds 2>$null) -join "`n"
    }
    $stats = @(& docker @compose stats --no-stream --format '{{.Name}}|{{.MemUsage}}' 2>$null) -join "`n"
    $dockerDf = @(& docker system df --format '{{.Type}}|{{.TotalCount}}|{{.Size}}|{{.Reclaimable}}' 2>$null) -join "`n"
    $imageRows = @(& docker image ls --filter 'reference=videoclip-mvp-stage*' --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' 2>$null)

    $row = [ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        interval_seconds = $IntervalSeconds
        e_free_bytes = [Int64](Get-PSDrive -Name E).Free
        business_root_bytes = Measure-DirectoryBytes $businessRoot
        stage2_app_bytes = Measure-DirectoryBytes $appRoot
        stage2_postgres_bytes = Measure-DirectoryBytes (Join-Path $businessRoot 'postgres')
        stage2_redis_bytes = Measure-DirectoryBytes (Join-Path $businessRoot 'redis')
        stage2_acceptance_bytes = Measure-DirectoryBytes (Join-Path $businessRoot 'acceptance')
        app_incoming_bytes = Measure-DirectoryBytes (Join-Path $appRoot 'incoming')
        app_analysis_bytes = Measure-DirectoryBytes (Join-Path $appRoot 'analysis')
        app_media_bytes = Measure-DirectoryBytes (Join-Path $appRoot 'media')
        app_leases_bytes = Measure-DirectoryBytes (Join-Path $appRoot 'leases')
        host_available_memory_bytes = [Int64](Get-CimInstance -ClassName Win32_OperatingSystem -Property FreePhysicalMemory).FreePhysicalMemory * 1024
        api_memory = Get-ContainerMetric 'api' $stats 1
        worker_memory = Get-ContainerMetric 'worker' $stats 1
        maintenance_worker_memory = Get-ContainerMetric 'maintenance-worker' $stats 1
        beat_memory = Get-ContainerMetric 'beat' $stats 1
        postgres_memory = Get-ContainerMetric 'postgres' $stats 1
        redis_memory = Get-ContainerMetric 'redis' $stats 1
        api_size_rw_bytes = Get-ContainerMetric 'api' $inspect 1
        worker_size_rw_bytes = Get-ContainerMetric 'worker' $inspect 1
        maintenance_worker_size_rw_bytes = Get-ContainerMetric 'maintenance-worker' $inspect 1
        beat_size_rw_bytes = Get-ContainerMetric 'beat' $inspect 1
        postgres_size_rw_bytes = Get-ContainerMetric 'postgres' $inspect 1
        redis_size_rw_bytes = Get-ContainerMetric 'redis' $inspect 1
        api_size_rootfs_bytes = Get-ContainerMetric 'api' $inspect 2
        worker_size_rootfs_bytes = Get-ContainerMetric 'worker' $inspect 2
        maintenance_worker_size_rootfs_bytes = Get-ContainerMetric 'maintenance-worker' $inspect 2
        beat_size_rootfs_bytes = Get-ContainerMetric 'beat' $inspect 2
        postgres_size_rootfs_bytes = Get-ContainerMetric 'postgres' $inspect 2
        redis_size_rootfs_bytes = Get-ContainerMetric 'redis' $inspect 2
        docker_images_size = ''
        docker_containers_size = ''
        docker_volumes_size = ''
        docker_build_cache_size = ''
        project_images = ($imageRows -join ';')
        api_log_bytes_proxy = -1
        worker_log_bytes_proxy = -1
        maintenance_worker_log_bytes_proxy = -1
        beat_log_bytes_proxy = -1
        postgres_log_bytes_proxy = -1
        redis_log_bytes_proxy = -1
        docker_log_file_exact_size_available = $false
    }

    foreach ($line in ($dockerDf -split "`r?`n")) {
        $parts = $line -split '\|', 4
        if ($parts.Count -lt 3) { continue }
        switch ($parts[0]) {
            'Images' { $row.docker_images_size = $parts[2] }
            'Containers' { $row.docker_containers_size = $parts[2] }
            'Local Volumes' { $row.docker_volumes_size = $parts[2] }
            'Build Cache' { $row.docker_build_cache_size = $parts[2] }
        }
    }

    foreach ($service in $services) {
        $id = (& docker @compose ps -q $service 2>$null | Select-Object -First 1).Trim()
        if (-not $id) { continue }
        $log = @(& docker logs --timestamps $id 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $bytes = [Text.Encoding]::UTF8.GetByteCount(($log -join "`n"))
            $logProperty = if ($service -eq 'maintenance-worker') { 'maintenance_worker_log_bytes_proxy' } else { "${service}_log_bytes_proxy" }
            $row[$logProperty] = [Int64]$bytes
        }
    }

    [PSCustomObject]$row | Export-Csv -LiteralPath $resolvedOutput -NoTypeInformation -Append -Encoding utf8
    Start-Sleep -Seconds $IntervalSeconds
}
