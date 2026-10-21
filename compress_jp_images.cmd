@echo off
setlocal

set "ROOT_DIR=%~dp0"
set "SCRIPT_PATH=%ROOT_DIR%JP\compress_source_images.py"
set "PYTHON_CMD="
set "PYTHON_ARGS="

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    set "PYTHON_ARGS=-3.12"
) else (
    set "PYTHON_CMD=python"
)

if not exist "%SCRIPT_PATH%" (
    echo Could not find "%SCRIPT_PATH%".
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo No arguments provided. Running JP image compression with script defaults.
    "%PYTHON_CMD%" %PYTHON_ARGS% "%SCRIPT_PATH%"
    set "EXIT_CODE=%errorlevel%"
    echo.
    echo Script finished with exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

"%PYTHON_CMD%" %PYTHON_ARGS% "%SCRIPT_PATH%" %*
set "EXIT_CODE=%errorlevel%"
echo.
echo Script finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
