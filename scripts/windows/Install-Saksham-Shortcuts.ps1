. (Join-Path $PSScriptRoot 'Saksham.Common.ps1')

try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $powershell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        $powershell = (Get-Command powershell.exe).Source
    }

    $shortcuts = @(
        @{
            Name = 'Start Saksham Server.lnk'
            Script = (Join-Path $PSScriptRoot 'Start-Saksham-Server.ps1')
            Description = 'Start Saksham RTX, LLM, Docker, and Tailscale services'
        },
        @{
            Name = 'Stop Saksham Server.lnk'
            Script = (Join-Path $PSScriptRoot 'Stop-Saksham-Server.ps1')
            Description = 'Safely stop Saksham server services and release the GPU'
        }
    )

    foreach ($item in $shortcuts) {
        $shortcutPath = Join-Path $desktop $item.Name
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powershell
        $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $item.Script
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.Description = $item.Description
        $shortcut.Save()
        Write-Host "Installed $shortcutPath"
    }
    Write-SakshamLog INFO 'Installed the Windows Start and Stop desktop shortcuts.'
} catch {
    Write-SakshamLog ERROR $_.Exception.Message
    Pause-OnLauncherFailure
    exit 1
}
