Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:SakshamRoot = if ($env:SAKSHAM_SERVER_ROOT) {
    $env:SAKSHAM_SERVER_ROOT
} else {
    'C:\Saksham'
}
$script:MeetingDirectory = Join-Path $script:SakshamRoot 'meeting_intelligence'
$script:MeetingEnv = Join-Path $script:MeetingDirectory '.env'
$script:LogDirectory = Join-Path $script:SakshamRoot 'logs'
$script:LauncherLog = Join-Path $script:LogDirectory 'launcher-windows.log'
$script:StatePath = Join-Path $script:SakshamRoot 'saksham-launcher-state.json'
$script:ExpectedTailIp = $env:SAKSHAM_TAILSCALE_IP
$script:MeetingPort = 8110
$script:LmStudioPort = 1234
$script:DefaultModel = 'openai/gpt-oss-20b'

New-Item -ItemType Directory -Path $script:LogDirectory -Force | Out-Null

function Write-SakshamLog {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    if ($Level -eq 'ERROR') {
        Write-Host $line -ForegroundColor Red
    } elseif ($Level -eq 'WARN') {
        Write-Host $line -ForegroundColor Yellow
    } else {
        Write-Host $line
    }
    Add-Content -LiteralPath $script:LauncherLog -Value $line
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $prefix = "$Name="
    $line = Get-Content -LiteralPath $Path | Where-Object { $_.StartsWith($prefix) } | Select-Object -Last 1
    if (-not $line) {
        return $null
    }
    $value = $line.Substring($prefix.Length).Trim()
    if ($value.Length -ge 2) {
        if (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
            ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    return $value
}

function Resolve-TailscaleCli {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'),
        (Join-Path $env:LOCALAPPDATA 'Tailscale\tailscale.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw 'Tailscale CLI was not found. Install or repair Tailscale, then retry.'
}

function Resolve-LmsCli {
    $command = Get-Command lms -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $candidates = @(
        (Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'),
        (Join-Path $env:USERPROFILE '.lmstudio\bin\lms.cmd')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw 'LM Studio CLI was not found. Open LM Studio once and install its lms CLI, then retry.'
}

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$BearerToken = '',
        [int]$TimeoutSeconds = 5
    )
    try {
        $parameters = @{
            Uri = $Uri
            Method = 'Get'
            TimeoutSec = $TimeoutSeconds
            UseBasicParsing = $true
        }
        if ($BearerToken) {
            $parameters.Headers = @{ Authorization = "Bearer $BearerToken" }
        }
        $response = Invoke-WebRequest @parameters
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$BearerToken = '',
        [int]$TimeoutSeconds = 120
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpEndpoint -Uri $Uri -BearerToken $BearerToken -TimeoutSeconds 5) {
            Write-SakshamLog INFO "$Name is ready."
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become ready at $Uri within $TimeoutSeconds seconds."
}

function Wait-DockerEngine {
    param([int]$TimeoutSeconds = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-SakshamLog INFO 'Docker Desktop Linux engine is ready.'
            return
        }
        Start-Sleep -Seconds 3
    }
    throw 'Docker Desktop did not become ready. Open Docker Desktop and confirm the WSL2 Linux engine is enabled.'
}

function Start-DockerDesktop {
    & docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-SakshamLog INFO 'Reusing the running Docker Desktop engine.'
        return
    }

    Write-SakshamLog INFO 'Starting Docker Desktop.'
    & docker desktop start --timeout 180 *> $null
    if ($LASTEXITCODE -ne 0) {
        $dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
            throw 'Docker Desktop could not be started and its application was not found.'
        }
        Start-Process -FilePath $dockerDesktop | Out-Null
    }
    Wait-DockerEngine
}

function Start-TailscaleConnection {
    param([Parameter(Mandatory = $true)][string]$Tailscale)
    & $Tailscale status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-SakshamLog INFO 'Bringing Tailscale online.'
        & $Tailscale up *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Tailscale could not connect. Open Tailscale and sign in, then retry.'
        }
    } else {
        Write-SakshamLog INFO 'Tailscale is connected.'
    }

    $tailIp = (& $Tailscale ip -4 | Select-Object -First 1).Trim()
    if ($script:ExpectedTailIp -and $tailIp -ne $script:ExpectedTailIp) {
        throw "Unexpected Tailscale IP '$tailIp'; expected '$($script:ExpectedTailIp)'. Update SAKSHAM_TAILSCALE_IP only if the device address intentionally changed."
    }
}

function Ensure-TailscaleForward {
    param(
        [Parameter(Mandatory = $true)][string]$Tailscale,
        [Parameter(Mandatory = $true)][int]$Port
    )
    $status = (& $Tailscale serve status 2>&1 | Out-String)
    if ($status -match ("tcp://[^\s:]+:{0}" -f $Port) -or
        $status -match ("100\.[^\s:]+:{0}" -f $Port)) {
        Write-SakshamLog INFO "Tailscale forwarding for port $Port is already configured."
        return
    }
    & $Tailscale serve --bg "--tcp=$Port" "tcp://127.0.0.1:$Port" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure private Tailscale forwarding for port $Port."
    }
    Write-SakshamLog INFO "Configured Tailscale forwarding for port $Port."
}

function Get-LmStudioModels {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:LmStudioPort)/v1/models" -Method Get -TimeoutSec 8
        return @($response.data | ForEach-Object { $_.id })
    } catch {
        return @()
    }
}

function Wait-MeetingWorkerIdle {
    param(
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [int]$TimeoutSeconds = 300
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$($script:MeetingPort)/health" `
                -Headers @{ Authorization = "Bearer $ApiKey" } `
                -Method Get `
                -TimeoutSec 8
            if (-not $health.busy) {
                return
            }
            Write-SakshamLog INFO 'Meeting worker is busy; waiting for the current job to finish.'
        } catch {
            return
        }
        Start-Sleep -Seconds 5
    }
    throw 'Meeting Intelligence is still processing. Stop the Mac only after processing finishes, then retry.'
}

function Save-LauncherState {
    param([Parameter(Mandatory = $true)][string]$Model)
    $state = [ordered]@{
        started_at = (Get-Date).ToString('o')
        tailscale_ip = $script:ExpectedTailIp
        model = $Model
        meeting_directory = $script:MeetingDirectory
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
}

function Pause-OnLauncherFailure {
    if ($Host.Name -eq 'ConsoleHost' -and -not $env:SAKSHAM_NO_PAUSE) {
        Read-Host 'Press Enter to close this window' | Out-Null
    }
}
