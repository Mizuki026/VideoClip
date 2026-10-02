$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stageRoot = Join-Path $repoRoot 'data\mvp-stage-2'
$secretDirectory = Join-Path $stageRoot 'secrets'
$postgresDirectory = Join-Path $stageRoot 'postgres'
$appDirectory = Join-Path $stageRoot 'app'
$environmentFile = Join-Path $stageRoot '.env'
$passwordFile = Join-Path $secretDirectory 'postgres_password'
$stage0Environment = Join-Path $repoRoot 'data\mvp-stage-0\.env'

function Get-PrivateDotEnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Private configuration is missing: $Path" }
    $prefix = [regex]::Escape("$Key=")
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^$prefix(.*)$") { return $Matches[1].Trim() }
    }
    throw "Required private setting is missing: $Key"
}

foreach ($directory in @(
    $secretDirectory,
    $postgresDirectory,
    (Join-Path $appDirectory 'incoming'),
    (Join-Path $appDirectory 'media'),
    (Join-Path $appDirectory 'leases'),
    (Join-Path $appDirectory 'analysis'),
    (Join-Path $stageRoot 'redis')
)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

if ((Test-Path -LiteralPath $environmentFile) -or (Test-Path -LiteralPath $passwordFile)) {
    throw 'Stage 2 private configuration already exists. Preserve it and do not regenerate database credentials.'
}

$freeConfirmed = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_FREE_USE_CONFIRMED'
$asrPrice = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_ASR_USD_PER_MINUTE'
$llmInputPrice = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_LLM_INPUT_USD_PER_MILLION'
$llmOutputPrice = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_LLM_OUTPUT_USD_PER_MILLION'
if ($freeConfirmed -ne 'I_CONFIRMED_BOTH_MODELS_ARE_FREE_IN_MY_ACCOUNT' -or
    $asrPrice -ne '0' -or $llmInputPrice -ne '0' -or $llmOutputPrice -ne '0') {
    throw 'The Stage 0 private config does not confirm zero-price ASR and LLM models. Recheck the account before running real model calls.'
}

$apiKey = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_API_KEY'
$asrModel = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_ASR_MODEL'
$llmModel = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_LLM_MODEL'
$baseUrl = Get-PrivateDotEnvValue $stage0Environment 'SILICONFLOW_BASE_URL'
$promptVersion = Get-PrivateDotEnvValue $stage0Environment 'PROMPT_VERSION'
$contextTokens = Get-PrivateDotEnvValue $stage0Environment 'LLM_CONTEXT_TOKENS'
$maxOutputTokens = Get-PrivateDotEnvValue $stage0Environment 'LLM_MAX_OUTPUT_TOKENS'
$httpTotal = Get-PrivateDotEnvValue $stage0Environment 'HTTP_TOTAL_TIMEOUT_SECONDS'
$httpConnect = Get-PrivateDotEnvValue $stage0Environment 'HTTP_CONNECT_TIMEOUT_SECONDS'
$httpRead = Get-PrivateDotEnvValue $stage0Environment 'HTTP_READ_TIMEOUT_SECONDS'
$responseBytes = Get-PrivateDotEnvValue $stage0Environment 'MAX_RESPONSE_BYTES'
$diagnosticBytes = Get-PrivateDotEnvValue $stage0Environment 'MAX_DIAGNOSTIC_BYTES'

$random = [Security.Cryptography.RandomNumberGenerator]::Create()
$databaseBytes = New-Object byte[] 32
$applicationBytes = New-Object byte[] 32
$random.GetBytes($databaseBytes)
$random.GetBytes($applicationBytes)
$random.Dispose()
$databasePassword = (($databaseBytes | ForEach-Object { $_.ToString('x2') }) -join '')
$applicationSecret = (($applicationBytes | ForEach-Object { $_.ToString('x2') }) -join '')
[IO.File]::WriteAllText($passwordFile, $databasePassword, [Text.Encoding]::ASCII)

$privateConfig = @"
APP_ENV=development
APP_ORIGIN=http://127.0.0.1:58002
APP_SECRET_KEY=$applicationSecret
APP_DATA_ROOT=E:/VideoClip/data/mvp-stage-2/app
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
SESSION_TTL_HOURS=24
MAX_SESSIONS_PER_USER=8
MAX_JSON_BODY_BYTES=16384
UPLOAD_TOTAL_TIMEOUT_SECONDS=1800
FFPROBE_TIMEOUT_SECONDS=45
FFPROBE_STOP_GRACE_SECONDS=5
CLEANUP_TIMEOUT_SECONDS=30
FFPROBE_PATH=/usr/bin/ffprobe
FFMPEG_PATH=/usr/bin/ffmpeg
DB_HOST=127.0.0.1
DB_PORT=55443
DB_NAME=videoclip_mvp_stage2
DB_USER=videoclip_stage2
DB_PASSWORD_FILE=E:/VideoClip/data/mvp-stage-2/secrets/postgres_password
POSTGRES_DB=videoclip_mvp_stage2
POSTGRES_USER=videoclip_stage2
MVP_STAGE1_DATA_DIR=E:/VideoClip/data/mvp-stage-2
MVP_STAGE1_ENV_FILE=E:/VideoClip/data/mvp-stage-2/.env
MVP_STAGE1_PG_PORT=55443
MVP_STAGE1_API_PORT=58002
MVP_IMAGE_NAME=videoclip-mvp-stage2-api:local
CELERY_BROKER_URL=redis://redis:6379/0
SILICONFLOW_API_KEY=$apiKey
SILICONFLOW_BASE_URL=$baseUrl
SILICONFLOW_ASR_MODEL=$asrModel
SILICONFLOW_LLM_MODEL=$llmModel
ASR_SECRET_REFERENCE=environment:SILICONFLOW_API_KEY
ASR_REQUEST_PARAMETERS={}
PROMPT_VERSION=$promptVersion
SUMMARY_SCHEMA_VERSION=summary-v1
LLM_CONTEXT_TOKENS=$contextTokens
LLM_MAX_OUTPUT_TOKENS=$maxOutputTokens
AUDIO_SAMPLE_RATE=16000
AUDIO_CHANNELS=1
AUDIO_SAMPLE_FORMAT=s16le
SEGMENT_SECONDS=30
TIMELINE_MAPPING_VERSION=zero-offset-v1
SLICE_MANIFEST_VERSION=pcm-s16le-slices-v1
HTTP_TOTAL_TIMEOUT_SECONDS=$httpTotal
HTTP_CONNECT_TIMEOUT_SECONDS=$httpConnect
HTTP_READ_TIMEOUT_SECONDS=$httpRead
MEDIA_TOTAL_TIMEOUT_SECONDS=300
MEDIA_STOP_GRACE_SECONDS=5
ATTEMPT_TOTAL_TIMEOUT_SECONDS=10800
MAX_RESPONSE_BYTES=$responseBytes
MAX_DIAGNOSTIC_BYTES=$diagnosticBytes
"@
[IO.File]::WriteAllText($environmentFile, $privateConfig.Trim() + "`n", [Text.UTF8Encoding]::new($false))

Write-Output 'Stage 2 directories and private configuration are ready. Secrets were not printed.'
