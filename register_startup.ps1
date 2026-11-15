param(
    [int]$DelaySeconds = 20,
    [switch]$Remove
)

# Registers (or removes with -Remove) a per-user scheduled task that starts the
# tray daemon at logon after a delay. The daemon then supervises the catalog
# web server and the OCR worker, exactly as when launched from open_lang.cmd,
# but without opening the catalog in the browser.

$ErrorActionPreference = "Stop"

$taskName = "Lang tray daemon"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$daemonDir = Join-Path $repoRoot "JP\desktop_capture_daemon"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed startup task '$taskName'."
    } else {
        Write-Host "Startup task '$taskName' is not registered."
    }
    return
}

$daemonExe = @(
    (Join-Path $daemonDir "target\release\desktop_capture_daemon.exe"),
    (Join-Path $daemonDir "target\debug\desktop_capture_daemon.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $daemonExe) {
    throw "desktop_capture_daemon.exe was not found. Run open_lang.cmd or setup_windows.ps1 -BuildDaemon once first."
}

# The daemon is a console binary; start it through a hidden PowerShell so no
# console window stays open, matching open_lang.cmd.
$launch = "Start-Process -FilePath '$daemonExe' -WorkingDirectory '$daemonDir' -WindowStyle Hidden"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$launch`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT${DelaySeconds}S"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the Lang tray daemon $DelaySeconds seconds after logon." `
    -Force | Out-Null

Write-Host "Registered startup task '$taskName': $daemonExe, $DelaySeconds s after logon."
