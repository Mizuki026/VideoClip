$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dataRoot = Join-Path $repoRoot 'data'
$stageRoot = Join-Path $dataRoot 'mvp-stage-0'
$dirs = @('input', 'work', 'postgres', 'redis', 'backup', 'backup\redis-restore', 'reports', 'secrets')
New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
foreach ($relative in $dirs) {
    New-Item -ItemType Directory -Force -Path (Join-Path $stageRoot $relative) | Out-Null
}

$sid = (whoami /user /fo csv | ConvertFrom-Csv).SID
$grantUser = "*$($sid):(OI)(CI)F"
$grantUserDirect = "*$($sid):F"
& icacls.exe $dataRoot /inheritance:r /grant:r $grantUser '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C | Out-Null
& icacls.exe $dataRoot /grant $grantUserDirect '*S-1-5-18:F' '*S-1-5-32-544:F' /T /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the probe data directory ACL.' }

$envFile = Join-Path $stageRoot '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $PSScriptRoot 'config.example.env') $envFile
}
$forwardStageRoot = $stageRoot.Replace('\', '/')
$envText = Get-Content -Raw $envFile
$envText = [regex]::Replace($envText, '(?m)^STAGE0_DATA_DIR=.*$', "STAGE0_DATA_DIR=$forwardStageRoot")
Set-Content -Path $envFile -Value $envText -Encoding utf8NoBOM

$secrets = Join-Path $stageRoot 'secrets'
$postgresPasswordFile = Join-Path $secrets 'postgres_password'
$redisConfigFile = Join-Path $secrets 'redis.conf'
if (-not (Test-Path $postgresPasswordFile)) {
    $pgPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    Set-Content -Path $postgresPasswordFile -Value $pgPassword -NoNewline -Encoding ascii
}
if (-not (Test-Path $redisConfigFile)) {
    $redisPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    Set-Content -Path $redisConfigFile -Value "requirepass $redisPassword`n" -Encoding ascii
}
& icacls.exe $dataRoot /inheritance:r /grant:r $grantUser '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C | Out-Null
& icacls.exe $dataRoot /grant $grantUserDirect '*S-1-5-18:F' '*S-1-5-32-544:F' /T /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not apply the private ACL to generated probe files.' }
Write-Output 'Private stage-0 data directories and local-only credentials are ready. Model API values remain unset.'
