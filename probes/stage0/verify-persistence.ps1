$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stageRoot = Join-Path $repoRoot 'data\mvp-stage-0'
$envFile = Join-Path $stageRoot '.env'
$composeFile = Join-Path $PSScriptRoot 'compose.yaml'
$redisConfig = Get-Content -Raw (Join-Path $stageRoot 'secrets\redis.conf')
$redisPassword = [regex]::Match($redisConfig, '(?m)^requirepass\s+([^\s]+)').Groups[1].Value
if (-not $redisPassword) { throw 'Redis password is missing from its private config.' }
$projectArgs = @('-p', 'videoclip-mvp-stage0', '--env-file', $envFile, '-f', $composeFile)

function Invoke-Compose([string[]]$Arguments) {
    $output = & docker compose @projectArgs @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Isolated Docker Compose operation failed ($($Arguments[0])); output suppressed." }
    return $output
}

function Invoke-Redis([string[]]$Arguments) {
    $output = & docker compose @projectArgs exec -T redis redis-cli --no-auth-warning -a $redisPassword @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Redis persistence probe failed; response suppressed.' }
    return (($output | Out-String).Trim())
}

function Wait-PostgresReady {
    for ($i = 0; $i -lt 45; $i++) {
        & docker compose @projectArgs exec -T postgres psql -U stage0_probe -d stage0_probe -Atqc 'SELECT 1' *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw 'PostgreSQL did not accept a query against the probe database within 45 seconds.'
}

function Wait-RedisRewrite([string]$operation, [string]$idleKey, [string]$statusKey) {
    $idlePattern = '(?m)^{0}:0\r?$' -f [regex]::Escape($idleKey)
    $statusPattern = '(?m)^{0}:ok\r?$' -f [regex]::Escape($statusKey)
    for ($i = 0; $i -lt 120; $i++) {
        $info = Invoke-Redis @('INFO', 'persistence')
        if ($info -match $idlePattern -and $info -match $statusPattern) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Redis background $operation did not finish successfully within 30 seconds."
}

try {
    Invoke-Compose @('up', '-d', 'postgres', 'redis') | Out-Null
    Wait-PostgresReady
    $createSql = "CREATE TABLE IF NOT EXISTS stage0_persistence_probe (probe_key text PRIMARY KEY, probe_value text NOT NULL); INSERT INTO stage0_persistence_probe VALUES ('persist-v1', 'E-drive-bind-mount') ON CONFLICT (probe_key) DO UPDATE SET probe_value = EXCLUDED.probe_value;"
    Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', 'stage0_probe', '-d', 'stage0_probe', '-v', 'ON_ERROR_STOP=1', '-c', $createSql) | Out-Null
    Invoke-Redis @('SET', 'stage0:persistence', 'E-drive-bind-mount') | Out-Null
    Invoke-Redis @('BGSAVE') | Out-Null
    Wait-RedisRewrite 'RDB save' 'rdb_bgsave_in_progress' 'rdb_last_bgsave_status'
    Invoke-Redis @('BGREWRITEAOF') | Out-Null
    Wait-RedisRewrite 'AOF rewrite' 'aof_rewrite_in_progress' 'aof_last_bgrewrite_status'
    Invoke-Compose @('exec', '-T', 'postgres', 'pg_dump', '-U', 'stage0_probe', '-Fc', '-f', '/stage0-backup/postgres.dump', 'stage0_probe') | Out-Null
    Invoke-Compose @('exec', '-T', 'postgres', 'dropdb', '--if-exists', '-U', 'stage0_probe', 'stage0_restore') | Out-Null
    Invoke-Compose @('exec', '-T', 'postgres', 'createdb', '-U', 'stage0_probe', 'stage0_restore') | Out-Null
    Invoke-Compose @('exec', '-T', 'postgres', 'pg_restore', '-U', 'stage0_probe', '-d', 'stage0_restore', '--no-owner', '/stage0-backup/postgres.dump') | Out-Null
    $restored = ((Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', 'stage0_probe', '-d', 'stage0_restore', '-At', '-c', "SELECT probe_value FROM stage0_persistence_probe WHERE probe_key = 'persist-v1'")) | Out-String).Trim()
    if ($restored -ne 'E-drive-bind-mount') { throw 'PostgreSQL backup restore did not retain its probe row.' }

    Invoke-Compose @('restart', 'postgres', 'redis') | Out-Null
    Wait-PostgresReady
    $pgPersisted = ((Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', 'stage0_probe', '-d', 'stage0_probe', '-At', '-c', "SELECT probe_value FROM stage0_persistence_probe WHERE probe_key = 'persist-v1'")) | Out-String).Trim()
    if ($pgPersisted -ne 'E-drive-bind-mount') { throw 'PostgreSQL bind-mounted data did not persist after restart.' }
    $redisPersisted = Invoke-Redis @('GET', 'stage0:persistence')
    if ($redisPersisted -ne 'E-drive-bind-mount') { throw 'Redis bind-mounted data did not persist after restart.' }

    $restoreDir = Join-Path $stageRoot 'backup\redis-restore'
    $rdbSource = Join-Path $stageRoot 'redis\dump.rdb'
    if (-not (Test-Path $rdbSource)) { throw 'Redis SAVE did not create the RDB file on the E drive.' }
    Copy-Item -Force $rdbSource (Join-Path $restoreDir 'dump.rdb')
    Invoke-Compose @('--profile', 'restore', 'up', '-d', 'redis-restore') | Out-Null
    try {
        $restoreValue = & docker compose @projectArgs --profile restore exec -T redis-restore redis-cli --no-auth-warning -a $redisPassword GET stage0:persistence 2>$null
        if ($LASTEXITCODE -ne 0 -or (($restoreValue | Out-String).Trim() -ne 'E-drive-bind-mount')) {
            throw 'Redis RDB restore did not retain its probe key.'
        }
    }
    finally {
        & docker compose @projectArgs --profile restore stop redis-restore *> $null
    }

    Write-Output 'PASS: PostgreSQL and Redis restart persistence; PostgreSQL pg_dump/pg_restore; Redis RDB restore.'
    Write-Output 'Evidence files are under data/mvp-stage-0 (PostgreSQL custom dump and Redis dump.rdb).'
}
finally {
    & docker compose @projectArgs stop postgres redis *> $null
}
