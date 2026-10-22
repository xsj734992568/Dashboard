@echo off
REM One-click deploy launcher. Pure ASCII on purpose: a .bat with non-ASCII
REM text hits the same codepage problem that broke deploy.ps1 before.
chcp 65001 >nul
cd /d "%~dp0"
echo Starting deploy...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -RepoName Dashboard
echo.
pause
