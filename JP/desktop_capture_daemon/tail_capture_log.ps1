param(
    [switch]$PrintPath
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$jpRoot = Split-Path -Parent $scriptDir
$configPath = Join-Path $scriptDir "capture_daemon.toml"
$defaultLogPath = Join-Path $jpRoot "data\capture_daemon.log"
$resolvedLogPath = [System.IO.Path]::GetFullPath($defaultLogPath)

if (Test-Path $configPath) {
    foreach ($line in Get-Content -Path $configPath) {
        if ($line -match '^\s*log_path\s*=\s*"([^"]+)"') {
            $candidate = $matches[1].Trim()
            if ($candidate) {
                if ([System.IO.Path]::IsPathRooted($candidate)) {
                    $resolvedLogPath = [System.IO.Path]::GetFullPath($candidate)
                } else {
                    $resolvedLogPath = [System.IO.Path]::GetFullPath(
                        (Join-Path $scriptDir $candidate)
                    )
                }
            }
            break
        }
    }
}

$logParent = Split-Path -Parent $resolvedLogPath
if ($logParent) {
    New-Item -ItemType Directory -Force -Path $logParent | Out-Null
}
if (-not (Test-Path $resolvedLogPath)) {
    New-Item -ItemType File -Force -Path $resolvedLogPath | Out-Null
}

if ($PrintPath) {
    Write-Output $resolvedLogPath
    return
}

try {
    $host.UI.RawUI.WindowTitle = "JP Capture Logs"
} catch {
}

Write-Host "Tailing $resolvedLogPath"
Write-Host "Press Ctrl+C to stop following the log."
Write-Host ""
Get-Content -Path $resolvedLogPath -Tail 80 -Wait
