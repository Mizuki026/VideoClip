$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stageRoot = Join-Path $repoRoot 'data\mvp-stage-1'
$secretDirectory = Join-Path $stageRoot 'secrets'
$postgresDirectory = Join-Path $stageRoot 'postgres'
$appDirectory = Join-Path $stageRoot 'app'
$environmentFile = Join-Path $stageRoot '.env'
$passwordFile = Join-Path $secretDirectory 'postgres_password'

New-Item -ItemType Directory -Force -Path $secretDirectory, $postgresDirectory, (Join-Path $appDirectory 'incoming'), (Join-Path $appDirectory 'media'), (Join-Path $appDirectory 'leases') | Out-Null

if (-not (Test-Path -LiteralPath $passwordFile)) {
    if (Test-Path -LiteralPath (Join-Path $postgresDirectory 'PG_VERSION')) {
        throw 'PostgreSQL data already exists but its password file is missing; restore the matching secret before continuing.'
    }
    $dbSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    [IO.File]::WriteAllText($passwordFile, $dbSecret, [Text.Encoding]::ASCII)
}

if (-not (Test-Path -LiteralPath $environmentFile)) {
    $example = Get-Content -Raw (Join-Path $repoRoot 'config.stage1.example.env')
    $appSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    $example = $example.Replace('APP_SECRET_KEY=replace-with-at-least-32-random-bytes', "APP_SECRET_KEY=$appSecret")
    $example += "`nMVP_STAGE1_DATA_DIR=E:/VideoClip/data/mvp-stage-1`nMVP_STAGE1_ENV_FILE=E:/VideoClip/data/mvp-stage-1/.env`nMVP_STAGE1_PG_PORT=55442`nMVP_STAGE1_API_PORT=58001`n"
    [IO.File]::WriteAllText($environmentFile, $example, [Text.UTF8Encoding]::new($false))
}

Write-Output 'Private Stage 1 directories and local configuration are ready. Secrets were not printed.'
