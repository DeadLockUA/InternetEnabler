# InternetEnabler client uninstaller.
# Run this from an elevated (Administrator) PowerShell prompt.
#
# Removes the auto-start Scheduled Task and the Windows Firewall rules
# created by install.ps1, and stops the running agent if any.

$ErrorActionPreference = "Stop"

$taskName = "InternetEnablerAgent"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentPath = Join-Path $scriptDir "agent.py"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this script from an elevated (Administrator) PowerShell window." -ForegroundColor Red
    exit 1
}

Write-Host "Stopping the agent (if running)..."
try {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like "*$agentPath*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
    Write-Host "  Stopped."
} catch {
    Write-Host "  No running agent process found."
}

Write-Host "Removing scheduled task '$taskName'..."
try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "  Removed."
} catch {
    Write-Host "  Scheduled task not found, skipping."
}

Write-Host "Removing firewall rules..."
netsh advfirewall firewall delete rule name="InternetEnabler-Block" | Out-Null
Write-Host $(if ($LASTEXITCODE -eq 0) { "  Removed InternetEnabler-Block." } else { "  InternetEnabler-Block not found, skipping." })
netsh advfirewall firewall delete rule name="InternetEnabler-Block-IPv6" | Out-Null
Write-Host $(if ($LASTEXITCODE -eq 0) { "  Removed InternetEnabler-Block-IPv6." } else { "  InternetEnabler-Block-IPv6 not found, skipping." })
netsh advfirewall firewall delete rule name="InternetEnabler-Inbound" | Out-Null
Write-Host $(if ($LASTEXITCODE -eq 0) { "  Removed InternetEnabler-Inbound." } else { "  InternetEnabler-Inbound not found, skipping." })

Write-Host "Done. config.json, schedule.json, tasks.json, history.json, messages.json and state.json were left in place -"
Write-Host "delete the client folder yourself if you want to remove those too."
