@echo off
setlocal

set "ROOT_DIR=%~dp0"
set "DAEMON_DIR=%ROOT_DIR%JP\desktop_capture_daemon"
set "RELEASE_EXE=%DAEMON_DIR%\target\release\desktop_capture_daemon.exe"
set "DEBUG_EXE=%DAEMON_DIR%\target\debug\desktop_capture_daemon.exe"
set "TRAY_EXE="
set "CARGO_AVAILABLE="

tasklist /FI "IMAGENAME eq desktop_capture_daemon.exe" | find /I "desktop_capture_daemon.exe" >nul
if not errorlevel 1 (
    exit /b 0
)

where cargo >nul 2>nul
if not errorlevel 1 (
    set "CARGO_AVAILABLE=1"
)

if defined CARGO_AVAILABLE (
    pushd "%DAEMON_DIR%"
    cargo build --release
    if errorlevel 1 (
        popd
        echo Failed to build the tray daemon.
        pause
        exit /b 1
    )
    popd
    set "TRAY_EXE=%RELEASE_EXE%"
) else if exist "%RELEASE_EXE%" (
    set "TRAY_EXE=%RELEASE_EXE%"
) else if exist "%DEBUG_EXE%" (
    set "TRAY_EXE=%DEBUG_EXE%"
) else (
    echo Could not find desktop_capture_daemon.exe and cargo is not available on PATH.
    echo Build the daemon once from %DAEMON_DIR% or install Rust/Cargo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%TRAY_EXE%' -WorkingDirectory '%DAEMON_DIR%' -WindowStyle Hidden"
if errorlevel 1 (
    echo Failed to launch the tray daemon.
    pause
    exit /b 1
)

exit /b 0
