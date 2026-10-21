@echo off
setlocal

powershell -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0JP\desktop_capture_daemon\tail_capture_log.ps1"
