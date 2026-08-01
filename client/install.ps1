# InternetEnabler client installer.
# Run this ONCE, from an elevated (Administrator) PowerShell prompt.
#
# It creates a Scheduled Task that starts the agent at logon with
# administrator rights (needed to toggle Windows Firewall rules), so the
# agent never has to show a UAC prompt during normal use.

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentPath = Join-Path $scriptDir "agent.py"
$configPath = Join-Path $scriptDir "config.json"
$taskName = "InternetEnablerAgent"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script from an elevated (Administrator) PowerShell window."
    exit 1
}

if (-not (Test-Path $configPath)) {
    Write-Host "config.json not found, copying from config.example.json - EDIT IT before continuing (token, lan_subnet)."
    Copy-Item (Join-Path $scriptDir "config.example.json") $configPath
    notepad $configPath
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
if ($config.token -eq "CHANGE_ME_SHARED_SECRET") {
    Write-Error "config.json still has the default token (CHANGE_ME_SHARED_SECRET). Edit it and set a real shared secret before installing."
    exit 1
}
try {
    [void][ipaddress]::Parse(($config.lan_subnet -split '/')[0])
} catch {
    Write-Error "config.json has an invalid lan_subnet: '$($config.lan_subnet)' (expected CIDR, e.g. 192.168.1.0/24)."
    exit 1
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $pythonw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) {
    Write-Error "Python not found on PATH. Install Python 3 first (https://www.python.org/downloads/)."
    exit 1
}

Write-Host "Installing dependencies..."
& python -m pip install -r (Join-Path $scriptDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed (exit code $LASTEXITCODE). Fix the error above and re-run this script."
    exit 1
}

# Use the interactively logged-on user, not $env:USERNAME - if this script was
# elevated via a "Run as a different user" admin credential, $env:USERNAME would
# be the admin account instead of the son's account, and the task would never
# trigger at his logon.
$loggedOnUser = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
if (-not $loggedOnUser) {
    $loggedOnUser = "$env:USERDOMAIN\$env:USERNAME"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$agentPath`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $loggedOnUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Scheduled task '$taskName' created for user '$loggedOnUser'. Starting the agent now..."
Start-ScheduledTask -TaskName $taskName

Write-Host "Done. The agent will start automatically at every logon (admin rights, no UAC prompt)."
Write-Host "Edit schedule.json (or use the server) to set daily block times."
