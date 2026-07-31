@echo off
:: Double-click this file to uninstall the InternetEnabler client.
:: Elevates to Administrator automatically, then runs uninstall.ps1
:: (stops the agent, removes the scheduled task and firewall rules).

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
pause
