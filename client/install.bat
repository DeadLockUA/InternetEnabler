@echo off
:: Double-click this file to install the InternetEnabler client.
:: Elevates to Administrator automatically, then runs install.ps1
:: (installs dependencies, sets up auto-start, launches the agent).

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
