param(
    [int]$TimeoutSeconds = 420,
    [switch]$SkipBackendTests
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$composeFile = Join-Path $repoRoot 'compose.yaml'
$overrideFile = Join-Path $PSScriptRoot 'final-stage5-acceptance.override.yaml'
$pythonFile = Join-Path $PSScriptRoot 'final-stage5-acceptance.py'
$python = Join-Path $repoRoot 'data\mvp-stage-2\.venv\Scripts\python.exe'
$runId = ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)).ToLowerInvariant()
$project = "videoclip-mvp-stage5-$runId"
$acceptanceRoot = Join-Path $repoRoot 'data\mvp-stage-5\acceptance'
$runRoot = Join-Path $acceptanceRoot $runId
$appRoot = Join-Path $runRoot 'app'
$secretRoot = Join-Path $runRoot 'secrets'
$environmentFile = Join-Path $runRoot '.env'
$outputDir = Join-Path $runRoot 'output'
$dataRoot = Join-Path $repoRoot 'data\mvp-stage-5'

function New-RandomHex([int]$ByteCount) {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes($ByteCount)
    try { return [Convert]::ToHexString($bytes).ToLowerInvariant() }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [int]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Convert-IPv4ToUInt32([string]$Address) {
    $bytes = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
    if ($bytes.Length -ne 4) { throw "Expected IPv4 address, got $Address" }
    return [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + [uint64]$bytes[3]
}

function Get-IPv4SubnetRange([string]$Subnet) {
    $parts = $Subnet.Split('/')
    if ($parts.Length -ne 2) { throw "Invalid Docker network subnet: $Subnet" }
    $prefix = [int]$parts[1]
    if ($prefix -lt 0 -or $prefix -gt 32) { throw "Invalid Docker network prefix: $Subnet" }
    $address = Convert-IPv4ToUInt32 $parts[0]
    $mask = if ($prefix -eq 0) { [uint64]0 } else { ([uint64]4294967295 -shl (32 - $prefix)) -band [uint64]4294967295 }
    $network = $address -band $mask
    return @($network, ($network + ([uint64]4294967295 - $mask)))
}

function Get-AvailableStage5Subnet {
    $usedSubnets = @()
    $networkIds = @(& docker network ls -q)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker networks before Stage 5 acceptance.' }
    foreach ($networkId in $networkIds) {
        $configured = @(& docker network inspect $networkId --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}')
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker IPAM ranges before Stage 5 acceptance.' }
        $usedSubnets += $configured | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+/\d+$' }
    }

    $start = Get-Random -Minimum 0 -Maximum 256
    for ($offset = 0; $offset -lt 256; $offset++) {
        $octet = ($start + $offset) % 256
        $candidate = "10.253.$octet.0/24"
        $candidateRange = Get-IPv4SubnetRange $candidate
        $overlaps = $false
        foreach ($used in $usedSubnets) {
            $usedRange = Get-IPv4SubnetRange $used
            if ($candidateRange[0] -le $usedRange[1] -and $usedRange[0] -le $candidateRange[1]) {
                $overlaps = $true
                break
            }
        }
        if (-not $overlaps) { return $candidate }
    }
    throw 'No unused 10.253.x.0/24 Docker subnet is available for Stage 5 acceptance.'
}

if (-not (Test-Path -LiteralPath $python)) { throw 'Stage 5 acceptance needs data/mvp-stage-2/.venv.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is required for isolated Stage 5 acceptance.' }
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'frontend\node_modules'))) { throw 'Run npm ci in frontend before browser acceptance.' }
if (Test-Path -LiteralPath $runRoot) { throw 'Refusing to reuse a Stage 5 acceptance directory.' }
$resolvedDataRoot = [System.IO.Path]::GetFullPath($dataRoot).TrimEnd('\') + '\'
$resolvedRunRoot = [System.IO.Path]::GetFullPath($runRoot)
if (-not $resolvedRunRoot.StartsWith($resolvedDataRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Computed Stage 5 data path is outside data/mvp-stage-5.'
}

New-Item -ItemType Directory -Path (Join-Path $runRoot 'postgres') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $runRoot 'redis') -Force | Out-Null
New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $appRoot 'stage5-stub') -Force | Out-Null
New-Item -ItemType Directory -Path $secretRoot -Force | Out-Null
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$databasePassword = New-RandomHex 32
$appSecret = New-RandomHex 48
$networkSubnet = Get-AvailableStage5Subnet
$passwordFile = Join-Path $secretRoot 'postgres_password'
[System.IO.File]::WriteAllText($passwordFile, $databasePassword, [System.Text.UTF8Encoding]::new($false))
$stageData = $runRoot.Replace('\', '/')
$pgPort = Get-FreeTcpPort
$apiPort = Get-FreeTcpPort
$webPort = Get-FreeTcpPort
$dbName = 'videoclip_mvp_stage5_acceptance'
$dbUser = 'videoclip_stage5'
$testDbName = 'videoclip_mvp_final_stage5_concurrency_test'
$settings = @(
    'APP_ENV=test',
    "APP_ORIGIN=http://127.0.0.1:$webPort",
    "APP_SECRET_KEY=$appSecret",
    'APP_DATA_ROOT=/appdata',
    'COOKIE_SECURE=false',
    'COOKIE_SAMESITE=lax',
    'DB_HOST=postgres',
    'DB_PORT=5432',
    "DB_NAME=$dbName",
    "DB_USER=$dbUser",
    'DB_PASSWORD_FILE=/run/secrets/postgres_password',
    "POSTGRES_DB=$dbName",
    "POSTGRES_USER=$dbUser",
    "MVP_STAGE1_DATA_DIR=$stageData",
    "MVP_STAGE1_ENV_FILE=$($environmentFile.Replace('\', '/'))",
    "MVP_STAGE1_PG_PORT=$pgPort",
    "MVP_STAGE1_API_PORT=$apiPort",
    "MVP_STAGE1_WEB_PORT=$webPort",
    "MVP_STAGE5_NETWORK_SUBNET=$networkSubnet",
    'MVP_IMAGE_NAME=videoclip-mvp-stage5-api:acceptance',
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
    'MODEL_COOLDOWN_BACKOFF_BASE_SECONDS=60',
    'MODEL_COOLDOWN_BACKOFF_MAX_SECONDS=900',
    'MODEL_WAIT_SINGLE_LIMIT_SECONDS=86400',
    'MODEL_WAIT_TOTAL_LIMIT_SECONDS=259200',
    'SILICONFLOW_ACCOUNT_REFERENCE=stage5-shared-account',
    'SILICONFLOW_API_KEY=stage5-local-substitute-only',
    'SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1',
    'SILICONFLOW_ASR_MODEL=stage5/local-asr-substitute',
    'SILICONFLOW_LLM_MODEL=Qwen/Qwen3.5-4B',
    'ASR_SECRET_REFERENCE=environment:SILICONFLOW_API_KEY',
    'ASR_REQUEST_PARAMETERS={}',
    'PROMPT_VERSION=stage0-summary-v5',
    'SUMMARY_SCHEMA_VERSION=summary-v1',
    'LLM_CONTEXT_TOKENS=256000',
    'LLM_MAX_OUTPUT_TOKENS=8192',
    'LLM_TEMPERATURE=0.2',
    'LLM_ENABLE_THINKING=false',
    'HTTP_TOTAL_TIMEOUT_SECONDS=120',
    'HTTP_CONNECT_TIMEOUT_SECONDS=1',
    'HTTP_READ_TIMEOUT_SECONDS=120',
    'LLM_HTTP_TOTAL_TIMEOUT_SECONDS=3',
    'LLM_HTTP_CONNECT_TIMEOUT_SECONDS=1',
    'LLM_HTTP_READ_TIMEOUT_SECONDS=2',
    'LLM_HTTP_WRITE_TIMEOUT_SECONDS=1',
    'LLM_HTTP_POOL_TIMEOUT_SECONDS=1',
    'SEGMENT_SECONDS=30',
    'AUDIO_SAMPLE_RATE=16000',
    'AUDIO_CHANNELS=1',
    'AUDIO_SAMPLE_FORMAT=s16le'
)
[System.IO.File]::WriteAllText($environmentFile, ($settings -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

$compose = @('compose', '--env-file', $environmentFile, '-p', $project, '-f', $composeFile, '-f', $overrideFile)
$workerInstanceId = [Guid]::NewGuid().ToString('D')
$succeeded = $false
$environmentNames = @(
    'ANALYSIS_WORKER_INSTANCE_ID', 'APP_ENV', 'APP_SECRET_KEY', 'APP_DATA_ROOT', 'DB_HOST', 'DB_PORT',
    'DB_NAME', 'DB_USER', 'DB_PASSWORD_FILE', 'VIDEOCLIP_TEST_DB_NAME', 'VIDEOCLIP_TEST_DATA_DIR', 'PYTHONPATH'
)
$savedEnvironment = @{}
foreach ($name in $environmentNames) {
    $entry = Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    $savedEnvironment[$name] = if ($null -eq $entry) { $null } else { $entry.Value }
}
Push-Location $repoRoot
try {
    & docker @compose build api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 5 image build failed: $LASTEXITCODE" }

    $certificateCommand = @'
set -eu
mkdir -p /appdata/stage5-stub
cd /appdata/stage5-stub
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 -subj '/CN=Stage5 Acceptance CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' -addext 'subjectKeyIdentifier=hash' -keyout acceptance-ca.key -out acceptance-ca.crt
openssl req -newkey rsa:2048 -nodes -sha256 -subj '/CN=api.siliconflow.cn' -keyout server.key -out server.csr
printf 'subjectAltName=DNS:api.siliconflow.cn\nextendedKeyUsage=serverAuth\n' > server.ext
openssl x509 -req -sha256 -days 2 -in server.csr -CA acceptance-ca.crt -CAkey acceptance-ca.key -CAcreateserial -extfile server.ext -out server.crt
cat server.crt acceptance-ca.crt > server-chain.pem
'@
    & docker @compose run --rm --no-deps --entrypoint sh -T api -ec $certificateCommand
    if ($LASTEXITCODE -ne 0) { throw "Could not create the local Stage 5 TLS substitute: $LASTEXITCODE" }

    $env:ANALYSIS_WORKER_INSTANCE_ID = $workerInstanceId
    & docker @compose up -d postgres redis model-stub
    if ($LASTEXITCODE -ne 0) { throw "Isolated Stage 5 dependencies did not start: $LASTEXITCODE" }
    $dependencyDeadline = [DateTime]::UtcNow.AddMinutes(5)
    do {
        $ids = @{}
        foreach ($service in @('postgres', 'redis', 'model-stub')) {
            $ids[$service] = (& docker @compose ps -q $service).Trim()
            if (-not $ids[$service]) { throw "Stage 5 dependency is missing: $service" }
        }
        $healthy = $true
        foreach ($service in @('postgres', 'redis', 'model-stub')) {
            $state = (& docker inspect --format '{{.State.Health.Status}}' $ids[$service]).Trim()
            $healthy = $healthy -and $state -eq 'healthy'
        }
        if ($healthy) { break }
        if ([DateTime]::UtcNow -ge $dependencyDeadline) { throw 'Timed out waiting for isolated Stage 5 dependencies.' }
        Start-Sleep -Seconds 2
    } while ($true)

    & docker @compose run --rm --no-deps --entrypoint python api -c "import urllib.request; assert urllib.request.urlopen('https://api.siliconflow.cn/health', timeout=5).status == 200"
    if ($LASTEXITCODE -ne 0) { throw 'Stage 5 API container could not verify TLS routing to the local model substitute.' }
    & docker @compose run --rm api alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Stage 5 application migration failed.' }
    & docker @compose up -d api worker maintenance-worker beat
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Stage 5 services did not start.' }

    & docker @compose exec -T postgres createdb -U $dbUser $testDbName
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the dedicated Stage 5 backend test database.' }

    if (-not $SkipBackendTests) {
        $env:APP_ENV = 'test'
        $env:APP_SECRET_KEY = $appSecret
        $env:APP_DATA_ROOT = Join-Path $runRoot 'test-data\app'
        $env:DB_HOST = '127.0.0.1'
        $env:DB_PORT = "$pgPort"
        $env:DB_NAME = $dbName
        $env:DB_USER = $dbUser
        $env:DB_PASSWORD_FILE = $passwordFile
        $env:VIDEOCLIP_TEST_DB_NAME = $testDbName
        $env:VIDEOCLIP_TEST_DATA_DIR = Join-Path $runRoot 'test-data'
        $env:PYTHONPATH = 'src'
        $backendOutput = Join-Path $outputDir 'pytest-backend.log'
        & $python -m pytest -q *>&1 | Tee-Object -FilePath $backendOutput
        if ($LASTEXITCODE -ne 0) { throw "Stage 5 backend test suite failed; see $backendOutput" }
    }

    & $python $pythonFile --project $project --environment-file $environmentFile --output-dir $outputDir --timeout-seconds $TimeoutSeconds --worker-instance-id $workerInstanceId
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Stage 5 model-wait and recovery acceptance failed.' }
    $succeeded = $true

    & docker @compose stop
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the completed isolated Stage 5 project cleanly.' }
    Write-Output "Stage 5 isolated acceptance passed. Evidence: $outputDir"
} finally {
    Pop-Location
    foreach ($name in $environmentNames) {
        if ($null -eq $savedEnvironment[$name]) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value $savedEnvironment[$name]
        }
    }
    if (-not $succeeded) {
        Write-Warning "Isolated Stage 5 project and data were preserved for inspection: $runRoot"
    }
}
