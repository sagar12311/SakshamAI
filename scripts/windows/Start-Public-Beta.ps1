# Dedicated public-beta launcher. Does not source the personal server launcher.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$envFile = Join-Path $root 'deploy\.env'
$compose = @('compose', '--env-file', $envFile, '-f', (Join-Path $root 'deploy\docker-compose.public.yml'))
$mutex = New-Object System.Threading.Mutex($false, 'Local\SakshamPublicBetaLauncher')
$locked = $false

function Read-Setting([string]$Name) {
    $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match ('^\s*' + [regex]::Escape($Name) + '=') } | Select-Object -Last 1
    if ($null -eq $line) { return '' }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}
function Check-Exit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}
function Wait-Ready([string]$Url, [int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $result = Invoke-RestMethod -Uri $Url -TimeoutSec 5 -Headers @{ 'ngrok-skip-browser-warning' = '1' }
            if ($result.status -eq 'ready') { return }
        } catch { }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Readiness check failed: $Url. Check Docker/ngrok; do not change or delete database volumes."
}

try {
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'The public-beta launcher is already running. Use its existing window.' }
    if (-not (Test-Path -LiteralPath $envFile)) { throw 'Missing deploy\.env. Complete the private configuration first.' }
    $publicUrl = Read-Setting 'NGROK_PUBLIC_URL'
    if ($publicUrl -notmatch '^https://[a-z0-9-]+\.ngrok-free\.(dev|app)$') {
        throw 'Set NGROK_PUBLIC_URL in deploy\.env to your assigned https://name.ngrok-free.dev address, without a trailing slash.'
    }
    if ((Read-Setting 'HOSTED_ENABLED') -ne 'true') { throw 'Set HOSTED_ENABLED=true after completing the inference test.' }
    $docker = Get-Command docker -ErrorAction Stop
    $ngrok = Get-Command ngrok -ErrorAction Stop
    $lms = Get-Command lms -ErrorAction SilentlyContinue
    if (-not $lms) {
        foreach ($candidate in @("$env:USERPROFILE\.lmstudio\bin\lms.exe", "$env:USERPROFILE\.lmstudio\bin\lms.cmd")) {
            if (Test-Path -LiteralPath $candidate) { $lms = Get-Command $candidate; break }
        }
    }
    if (-not $lms) { throw 'LM Studio CLI (lms) is missing. Install its CLI from LM Studio, then retry.' }

    Write-Host 'Checking Docker Desktop...'
    & $docker.Source info *> $null
    if ($LASTEXITCODE -ne 0) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $desktop)) { throw 'Docker Desktop was not found.' }
        Start-Process -FilePath $desktop | Out-Null
        $deadline = (Get-Date).AddMinutes(3)
        do {
            Start-Sleep -Seconds 3
            & $docker.Source info *> $null
            if ($LASTEXITCODE -eq 0) { break }
        } while ((Get-Date) -lt $deadline)
        Check-Exit 'Docker did not become ready. Open Docker Desktop and check WSL2.'
    }
    Write-Host 'Starting the public database and gateway (existing data is preserved)...'
    & $docker.Source @compose up -d postgres gateway
    Check-Exit 'Public gateway startup failed. Check deploy\.env and gateway logs.'
    Wait-Ready 'http://127.0.0.1:8080/ready'
    $authCode = & $docker.Source @compose exec -T gateway python -c "import httpx; print(httpx.get('http://127.0.0.1:8080/v1/projects').status_code)"
    Check-Exit 'Could not check gateway authentication.'
    if ("$authCode".Trim() -ne '401') { throw 'Gateway authentication check failed. Tunnel will not be started.' }

    Write-Host 'Starting LM Studio and checking the configured model...'
    & $lms.Source daemon up
    Check-Exit 'LM Studio daemon could not start.'
    $listener = Get-NetTCPConnection -LocalPort 1234 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) {
        & $lms.Source server start --port 1234
        Check-Exit 'LM Studio server could not start on port 1234.'
    }
    # Read the private credentials inside the container, never through command output.
    $probe = @'
import os, sys, httpx
base = os.environ['HOSTED_LLM_BASE_URL'].rstrip('/').removesuffix('/v1')
key = os.environ.get('HOSTED_LLM_API_KEY', '')
try:
    r = httpx.get(base+'/api/v0/models', headers={'Authorization': 'Bearer '+key} if key else {}, timeout=10)
    r.raise_for_status()
    model = next((m for m in r.json()['data'] if m['id'] == os.environ['HOSTED_LLM_MODEL']), None)
    sys.exit(0 if model and model.get('state') == 'loaded' else 10)
except Exception:
    print('Cannot reach LM Studio model metadata. Check its server authentication and network settings.')
    sys.exit(1)
'@
    & $docker.Source @compose exec -T gateway python -c $probe
    if ($LASTEXITCODE -eq 10) {
        $model = Read-Setting 'HOSTED_LLM_MODEL'
        if (-not $model) { $model = 'openai/gpt-oss-20b' }
        & $lms.Source load $model --identifier $model --yes
        Check-Exit 'Model could not load. Check available GPU memory in LM Studio.'
        & $docker.Source @compose exec -T gateway python -c $probe
    }
    Check-Exit 'The configured model is not ready. Tunnel will not be started.'

    # Never kill an existing agent or silently create a second tunnel.
    $agent = $null
    try { $agent = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3 } catch { }
    if ($agent) {
        $matches = @($agent.tunnels | Where-Object {
            $_.public_url.TrimEnd('/') -eq $publicUrl -and $_.config.addr -in @('http://127.0.0.1:8080', 'http://localhost:8080')
        })
        if ($matches.Count -ne 1) { throw 'Another ngrok configuration is already running. Check its terminal; it was not stopped.' }
        Wait-Ready "$publicUrl/ready" 30
        Write-Host 'Public beta is ready. Reusing the existing ngrok window; keep that window open.' -ForegroundColor Green
        Read-Host 'Press Enter to close this launcher (services will keep running)'
    } else {
        Write-Host 'Starting the public tunnel. Keep THIS window open; closing it stops the tunnel.' -ForegroundColor Yellow
        Write-Host 'Website: https://sakshampublicbeta.pages.dev'
        & $ngrok.Source http http://127.0.0.1:8080 --url $publicUrl --inspect=false
        Check-Exit 'ngrok stopped with an error. Check its message and account configuration.'
    }
} catch {
    Write-Host "Public beta startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
