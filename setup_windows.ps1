param(
    [string]$PythonExe,
    [string]$CapturePythonExe,
    [string]$MainVenvPath = ".venv",
    [string]$CaptureVenvPath = ".venv-capture312",
    [switch]$InstallCaptureDeps,
    [switch]$BuildDaemon,
    [switch]$RunSmokeTests
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$jpDir = Join-Path $repoRoot "JP"
$mainRequirements = Join-Path $jpDir "requirements-main.txt"
$captureRequirements = Join-Path $jpDir "requirements-capture.txt"

function Resolve-PythonCommand {
    param(
        [string]$ExplicitValue
    )

    if ($ExplicitValue) {
        return [pscustomobject]@{
            Command = $ExplicitValue
            PrefixArguments = @()
        }
    }

    $candidates = @(
        @{ Command = "py"; PrefixArguments = @("-3.12") },
        @{ Command = "py"; PrefixArguments = @("-3.11") },
        @{ Command = "python"; PrefixArguments = @() }
    )

    foreach ($candidate in $candidates) {
        try {
            $prefixArguments = @($candidate.PrefixArguments)
            & $candidate.Command @prefixArguments --version | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]$candidate
            }
        } catch {
        }
    }

    throw "No Python interpreter was found. Install Python first or pass -PythonExe."
}

function Invoke-PythonCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArguments = @(),
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Command @PrefixArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $Command $($PrefixArguments + $Arguments -join ' ')"
    }
}

function New-ProjectVenv {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$PythonCommand,
        [Parameter(Mandatory = $true)]
        [string]$VenvPath
    )

    $absoluteVenvPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $VenvPath))
    $absoluteRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
    if (-not $absoluteVenvPath.StartsWith($absoluteRepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The venv path must stay inside the repository: $absoluteVenvPath"
    }

    $venvPython = Join-Path $absoluteVenvPath "Scripts\python.exe"
    $shouldCreate = -not (Test-Path $absoluteVenvPath)
    if (-not $shouldCreate -and (Test-Path $venvPython)) {
        try {
            & $venvPython --version | Out-Null
            $shouldCreate = $LASTEXITCODE -ne 0
        } catch {
            $shouldCreate = $true
        }
    } elseif (-not $shouldCreate) {
        $shouldCreate = $true
    }

    if ($shouldCreate -and (Test-Path $absoluteVenvPath)) {
        Write-Host "Recreating broken venv at $absoluteVenvPath"
        Remove-Item -LiteralPath $absoluteVenvPath -Recurse -Force
    }

    if ($shouldCreate) {
        Write-Host "Creating venv at $absoluteVenvPath"
        Invoke-PythonCommand `
            -Command $PythonCommand.Command `
            -PrefixArguments $PythonCommand.PrefixArguments `
            -Arguments @("-m", "venv", $absoluteVenvPath)
    } else {
        Write-Host "Reusing existing venv at $absoluteVenvPath"
    }

    if (-not (Test-Path $venvPython)) {
        throw "Virtual environment is missing python.exe: $venvPython"
    }
    return $venvPython
}

function Install-Requirements {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath,
        [Parameter(Mandatory = $true)]
        [string]$RequirementsPath
    )

    Write-Host "Installing requirements from $RequirementsPath"
    Invoke-PythonCommand -Command $PythonPath -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-PythonCommand -Command $PythonPath -Arguments @("-m", "pip", "install", "-r", $RequirementsPath)
}

$resolvedMainPython = Resolve-PythonCommand -ExplicitValue $PythonExe
$mainVenvPython = New-ProjectVenv -PythonCommand $resolvedMainPython -VenvPath $MainVenvPath
Install-Requirements -PythonPath $mainVenvPython -RequirementsPath $mainRequirements

if ($InstallCaptureDeps) {
    $resolvedCapturePython = Resolve-PythonCommand -ExplicitValue $CapturePythonExe
    $captureVenvPython = New-ProjectVenv -PythonCommand $resolvedCapturePython -VenvPath $CaptureVenvPath
    Install-Requirements -PythonPath $captureVenvPython -RequirementsPath $captureRequirements
}

if ($BuildDaemon) {
    $cargo = Get-Command cargo -ErrorAction SilentlyContinue
    if (-not $cargo) {
        throw "cargo is not available on PATH. Install Rust first or rerun without -BuildDaemon."
    }
    Push-Location (Join-Path $jpDir "desktop_capture_daemon")
    try {
        & $cargo.Source build --release
        if ($LASTEXITCODE -ne 0) {
            throw "cargo build --release failed."
        }
    } finally {
        Pop-Location
    }
}

if ($RunSmokeTests) {
    Write-Host "Running smoke tests"
    Invoke-PythonCommand -Command $mainVenvPython -Arguments @(
        "-m",
        "unittest",
        "JP.test_term_blacklist",
        "JP.test_lang_jp_web"
    )
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Main Python: $mainVenvPython"
if ($InstallCaptureDeps) {
    Write-Host "Capture Python: $(Join-Path (Join-Path $repoRoot $CaptureVenvPath) 'Scripts\python.exe')"
}
if ($BuildDaemon) {
    Write-Host "Daemon build: JP\desktop_capture_daemon\target\release\desktop_capture_daemon.exe"
}
