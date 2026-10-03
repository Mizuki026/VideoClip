param(
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$composeFile = Join-Path $repoRoot 'compose.yaml'
$overrideFile = Join-Path $PSScriptRoot 'queue-coordination-acceptance.override.yaml'
$pythonFile = Join-Path $PSScriptRoot 'queue-coordination-acceptance.py'
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$runId = ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)).ToLowerInvariant()
$project = "videoclip-mvp-stage4-$runId"
$runRoot = Join-Path $repoRoot "data\mvp-stage-4\acceptance\$runId"
$appRoot = Join-Path $runRoot 'app'
$secretRoot = Join-Path $runRoot 'secrets'
$environmentFile = Join-Path $runRoot '.env'
$outputDir = Join-Path $runRoot 'output'

function New-RandomHex([int]$ByteCount) {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes($ByteCount)
    try { return [Convert]::ToHexString($bytes).ToLowerInvariant() }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}

if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv pip sync with requirements-dev.lock first.' }
if (Test-Path -LiteralPath $runRoot) { throw 'Refusing to reuse a Stage 4 acceptance data directory.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is required for the isolated Stage 4 chain.' }

New-Item -ItemType Directory -Path (Join-Path $runRoot 'postgres') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $runRoot 'redis') -Force | Out-Null
New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $appRoot 'stage4-stub') -Force | Out-Null
New-Item -ItemType Directory -Path $secretRoot -Force | Out-Null
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$databasePassword = New-RandomHex 32
$appSecret = New-RandomHex 48
$passwordFile = Join-Path $secretRoot 'postgres_password'
[System.IO.File]::WriteAllText($passwordFile, $databasePassword, [System.Text.UTF8Encoding]::new($false))
$stageData = $runRoot.Replace('\', '/')
$pgPort = Get-FreeTcpPort
$apiPort = Get-FreeTcpPort
$webPort = Get-FreeTcpPort
$settings = @(
    'APP_ENV=test',
    "APP_ORIGIN=http://127.0.0.1:$webPort",
    "APP_SECRET_KEY=$appSecret",
    'APP_DATA_ROOT=/appdata',
    'COOKIE_SECURE=false',
    'COOKIE_SAMESITE=lax',
    "DB_HOST=postgres",
    'DB_PORT=5432',
    'DB_NAME=videoclip_mvp_stage4_acceptance',
    'DB_USER=videoclip_stage4',
    'DB_PASSWORD_FILE=/run/secrets/postgres_password',
    'POSTGRES_DB=videoclip_mvp_stage4_acceptance',
    'POSTGRES_USER=videoclip_stage4',
    "MVP_STAGE1_DATA_DIR=$stageData",
    "MVP_STAGE1_ENV_FILE=$($environmentFile.Replace('\', '/'))",
    "MVP_STAGE1_PG_PORT=$pgPort",
    "MVP_STAGE1_API_PORT=$apiPort",
    "MVP_STAGE1_WEB_PORT=$webPort",
    'MVP_IMAGE_NAME=videoclip-mvp-stage4-api:acceptance',
    'CELERY_BROKER_URL=redis://redis:6379/0',
    'UPLOAD_RECLAIM_BATCH_SIZE=4',
    'UPLOAD_RECLAIM_INTERVAL_SECONDS=3600',
    'ANALYSIS_DELIVERY_RETRY_LIMIT=8',
    'ANALYSIS_DELIVERY_DEADLINE_SECONDS=86400',
    'ANALYSIS_DELIVERY_SCAN_LIMIT=4',
    'ANALYSIS_DELIVERY_INTERVAL_SECONDS=60',
    'ANALYSIS_DELIVERY_OPERATION_TIMEOUT_SECONDS=60',
    'ANALYSIS_DELIVERY_BACKOFF_BASE_SECONDS=60',
    'ANALYSIS_DELIVERY_BACKOFF_MAX_SECONDS=900',
    'SILICONFLOW_API_KEY=stage4-controlled-local-substitute-only',
    'SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1',
    'SILICONFLOW_ASR_MODEL=stage4/local-asr-substitute',
    'SILICONFLOW_LLM_MODEL=Qwen/Qwen3.5-4B',
    'ASR_SECRET_REFERENCE=environment:SILICONFLOW_API_KEY',
    'ASR_REQUEST_PARAMETERS={}',
    'PROMPT_VERSION=stage0-summary-v5',
    'SUMMARY_SCHEMA_VERSION=summary-v1',
    'LLM_CONTEXT_TOKENS=256000',
    'LLM_MAX_OUTPUT_TOKENS=8192',
    'LLM_TEMPERATURE=0.2',
    'LLM_ENABLE_THINKING=false',
    'HTTP_READ_TIMEOUT_SECONDS=180',
    'SEGMENT_SECONDS=30',
    'AUDIO_SAMPLE_RATE=16000',
    'AUDIO_CHANNELS=1',
    'AUDIO_SAMPLE_FORMAT=s16le'
)
[System.IO.File]::WriteAllText(
    $environmentFile,
    ($settings -join "`n") + "`n",
    [System.Text.UTF8Encoding]::new($false)
)
$env:ANALYSIS_WORKER_INSTANCE_ID = [Guid]::NewGuid().ToString('D')

$compose = @(
    'compose', '--env-file', $environmentFile, '-p', $project,
    '-f', $composeFile, '-f', $overrideFile
)
$succeeded = $false
Push-Location $repoRoot
try {
    & docker @compose build api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 4 image build failed: $LASTEXITCODE" }

    $certificateCommand = @'
set -eu
mkdir -p /appdata/stage4-stub
cd /appdata/stage4-stub
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 -subj '/CN=Stage4 Acceptance CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' -addext 'subjectKeyIdentifier=hash' -keyout acceptance-ca.key -out acceptance-ca.crt
openssl req -newkey rsa:2048 -nodes -sha256 -subj '/CN=api.siliconflow.cn' -keyout server.key -out server.csr
printf 'subjectAltName=DNS:api.siliconflow.cn\nextendedKeyUsage=serverAuth\n' > server.ext
openssl x509 -req -sha256 -days 2 -in server.csr -CA acceptance-ca.crt -CAkey acceptance-ca.key -CAcreateserial -extfile server.ext -out server.crt
cat server.crt acceptance-ca.crt > server-chain.pem
'@
    & docker @compose run --rm --no-deps --entrypoint sh -T api -ec $certificateCommand
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated local model substitute certificate: $LASTEXITCODE" }

    & docker @compose up -d postgres redis model-stub
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 4 dependencies did not start: $LASTEXITCODE" }
    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    do {
        $postgresId = (& docker @compose ps -q postgres).Trim()
        $redisId = (& docker @compose ps -q redis).Trim()
        $stubId = (& docker @compose ps -q model-stub).Trim()
        if (-not $postgresId -or -not $redisId -or -not $stubId) { throw 'Isolated Stage 4 dependencies are missing.' }
        $postgresHealth = (& docker inspect --format '{{.State.Health.Status}}' $postgresId).Trim()
        $redisHealth = (& docker inspect --format '{{.State.Health.Status}}' $redisId).Trim()
        $stubHealth = (& docker inspect --format '{{.State.Health.Status}}' $stubId).Trim()
        if ($postgresHealth -eq 'healthy' -and $redisHealth -eq 'healthy' -and $stubHealth -eq 'healthy') { break }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Timed out waiting for isolated Stage 4 dependencies.' }
        Start-Sleep -Seconds 2
    } while ($true)

    & docker @compose run --rm --no-deps api python -c "import urllib.request; response=urllib.request.urlopen('https://api.siliconflow.cn/health', timeout=5); assert response.status == 200"
    if ($LASTEXITCODE -ne 0) { throw "Isolated local model TLS routing failed: $LASTEXITCODE" }

    & docker @compose run --rm api alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 4 migration failed: $LASTEXITCODE" }
    & docker @compose up -d api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 4 services did not start: $LASTEXITCODE" }

    $healthDeadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/health" -TimeoutSec 3
            if ($health.status -eq 'ok') { break }
        } catch { }
        if ([DateTime]::UtcNow -ge $healthDeadline) { throw 'Isolated Stage 4 API did not become healthy.' }
        Start-Sleep -Seconds 2
    } while ($true)

    & $python $pythonFile --project $project --environment-file $environmentFile --output-dir $outputDir --timeout-seconds $TimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 4 queue acceptance failed: $LASTEXITCODE" }
    $succeeded = $true

    & docker @compose stop
    if ($LASTEXITCODE -ne 0) { throw "Could not stop the idle isolated Stage 4 services cleanly: $LASTEXITCODE" }
    Write-Output "Stage 4 isolated acceptance passed. Evidence: $outputDir"
} finally {
    Pop-Location
    Remove-Item Env:ANALYSIS_WORKER_INSTANCE_ID -ErrorAction SilentlyContinue
    if (-not $succeeded) {
        Write-Warning "Isolated Stage 4 stack and data were preserved for inspection: $runRoot"
    }
}
