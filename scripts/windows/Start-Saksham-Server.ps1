param(
    [string]$Model = 'openai/gpt-oss-20b'
)

. (Join-Path $PSScriptRoot 'Saksham.Common.ps1')

try {
    Write-SakshamLog INFO 'Starting the Saksham Windows server stack.'
    if (-not (Test-Path -LiteralPath $script:MeetingDirectory -PathType Container)) {
        throw "Meeting worker directory is missing: $($script:MeetingDirectory)"
    }
    if (-not (Test-Path -LiteralPath $script:MeetingEnv -PathType Leaf)) {
        throw "Meeting worker configuration is missing: $($script:MeetingEnv)"
    }
    $meetingKey = Get-DotEnvValue -Path $script:MeetingEnv -Name 'MEETING_INTELLIGENCE_API_KEY'
    if (-not $meetingKey) {
        throw 'MEETING_INTELLIGENCE_API_KEY is missing from the worker .env file.'
    }
    $hfToken = Get-DotEnvValue -Path $script:MeetingEnv -Name 'HF_TOKEN'
    if (-not $hfToken -or $hfToken.StartsWith('replace-')) {
        throw 'HF_TOKEN is missing from the worker .env file. Accept the required Hugging Face model conditions, then add the token before starting.'
    }

    $tailscale = Resolve-TailscaleCli
    Start-TailscaleConnection -Tailscale $tailscale
    Start-DockerDesktop

    Push-Location $script:MeetingDirectory
    try {
        & docker compose up --build -d
        if ($LASTEXITCODE -ne 0) {
            throw 'Docker Compose could not start the Meeting Intelligence worker.'
        }
    } finally {
        Pop-Location
    }
    Wait-HttpEndpoint `
        -Name 'CUDA Meeting Intelligence' `
        -Uri "http://127.0.0.1:$($script:MeetingPort)/health" `
        -BearerToken $meetingKey `
        -TimeoutSeconds 180
    $workerHealth = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$($script:MeetingPort)/health" `
        -Method Get `
        -Headers @{ Authorization = "Bearer $meetingKey" } `
        -TimeoutSec 30
    if (-not $workerHealth.capabilities.admin_voice_auth) {
        throw 'Meeting worker is online, but admin voice authentication did not pass its model readiness check.'
    }
    Write-SakshamLog INFO 'Admin voice authentication model is ready.'

    $lms = Resolve-LmsCli
    & $lms daemon up *> $null
    if (-not (Test-HttpEndpoint -Uri "http://127.0.0.1:$($script:LmStudioPort)/v1/models" -TimeoutSeconds 5)) {
        & $lms server start --port $script:LmStudioPort *> $null
    }
    Wait-HttpEndpoint `
        -Name 'LM Studio server' `
        -Uri "http://127.0.0.1:$($script:LmStudioPort)/v1/models" `
        -TimeoutSeconds 90

    $loadedModels = @(Get-LmStudioModels)
    if ($loadedModels -notcontains $Model) {
        Write-SakshamLog INFO "Loading $Model with maximum GPU offload."
        & $lms load $Model --gpu=max
        if ($LASTEXITCODE -ne 0) {
            throw "LM Studio could not load '$Model'. Confirm it is downloaded under that model key."
        }
    } else {
        Write-SakshamLog INFO "Reusing the loaded LM Studio model $Model."
    }

    $deadline = (Get-Date).AddSeconds(180)
    do {
        $loadedModels = @(Get-LmStudioModels)
        if ($loadedModels -contains $Model) {
            break
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    if ($loadedModels -notcontains $Model) {
        throw "LM Studio is running, but '$Model' is not available through /v1/models."
    }

    Ensure-TailscaleForward -Tailscale $tailscale -Port $script:MeetingPort
    Ensure-TailscaleForward -Tailscale $tailscale -Port $script:LmStudioPort
    Save-LauncherState -Model $Model
    Write-SakshamLog INFO 'Saksham server is ready. You can now click Start Saksham on the Mac.'
} catch {
    Write-SakshamLog ERROR $_.Exception.Message
    Write-SakshamLog ERROR "Inspect $($script:LauncherLog) for the startup timeline."
    Pause-OnLauncherFailure
    exit 1
}
