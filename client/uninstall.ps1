# InternetEnabler client uninstaller.
# Run this from an elevated (Administrator) PowerShell prompt.
#
# Removes the auto-start Scheduled Task and the Windows Firewall rules
# created by install.ps1, and stops the running agent if any.

$ErrorActionPreference = "SilentlyContinue"

$taskName = "InternetEnablerAgent"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script from an elevated (Administrator) PowerShell window."
    exit 1
}

Write-Host "Stopping the agent (if running)..."
Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine -like "*agent.py*" } |
    Stop-Process -Force

Write-Host "Removing scheduled task '$taskName'..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false

Write-Host "Removing firewall rules..."
netsh advfirewall firewall delete rule name="InternetEnabler-Block" | Out-Null
netsh advfirewall firewall delete rule name="InternetEnabler-Inbound" | Out-Null

Write-Host "Done. config.json, schedule.json, tasks.json and history.json were left in place -"
Write-Host "delete the client folder yourself if you want to remove those too."
