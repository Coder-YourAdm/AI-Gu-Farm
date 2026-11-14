@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0START_DEMO.ps1"
echo.
echo Demo server stopped. See the message above if startup failed.
pause
