param(
    [int]$WorkerWaitSeconds = 300
)

. (Join-Path $PSScriptRoot 'Saksham.Common.ps1')

try {
    Write-SakshamLog INFO 'Stopping the Saksham Windows server stack.'
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) {
        throw 'No Saksham launcher state was found. Run Start Saksham Server once before using this shortcut.'
    }

    $meetingKey = Get-DotEnvValue -Path $script:MeetingEnv -Name 'MEETING_INTELLIGENCE_API_KEY'
    if ($meetingKey) {
        Wait-MeetingWorkerIdle -ApiKey $meetingKey -TimeoutSeconds $WorkerWaitSeconds
    }

    $lms = $null
    try {
        $lms = Resolve-LmsCli
    } catch {
        Write-SakshamLog WARN $_.Exception.Message
    }
    if ($lms) {
        & $lms unload --all *> $null
        & $lms server stop *> $null
        & $lms daemon down *> $null
        Write-SakshamLog INFO 'LM Studio server stopped and models unloaded.'
    }

    & docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Push-Location $script:MeetingDirectory
        try {
            & docker compose stop
            if ($LASTEXITCODE -ne 0) {
                throw 'Docker Compose could not stop the Meeting Intelligence worker.'
            }
        } finally {
            Pop-Location
        }
        & docker desktop stop --timeout 120 *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-SakshamLog WARN 'Docker Desktop did not acknowledge the stop command; close it from the tray if it remains open.'
        } else {
            Write-SakshamLog INFO 'Meeting worker and Docker Desktop stopped.'
        }
    }

    $tailscale = Resolve-TailscaleCli
    & $tailscale down *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Tailscale could not be taken offline.'
    }
    Write-SakshamLog INFO 'Tailscale is offline. Persistent Serve routes remain saved for the next startup.'
    Remove-Item -LiteralPath $script:StatePath -Force
    Write-SakshamLog INFO 'Saksham Windows server stack is stopped.'
} catch {
    Write-SakshamLog ERROR $_.Exception.Message
    Write-SakshamLog ERROR 'No remaining services were force-terminated.'
    Pause-OnLauncherFailure
    exit 1
}
