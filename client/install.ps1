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
$logPath = Join-Path $scriptDir "agent.log"
$taskName = "InternetEnablerAgent"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script from an elevated (Administrator) PowerShell window."
    exit 1
}

if (-not (Test-Path $configPath)) {
    Write-Host "config.json not found, copying from config.example.json - EDIT IT before continuing (token, web_password, lan_subnet)."
    Copy-Item (Join-Path $scriptDir "config.example.json") $configPath
    notepad $configPath
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
if ($config.token -eq "CHANGE_ME_SHARED_SECRET") {
    Write-Error "config.json still has the default token (CHANGE_ME_SHARED_SECRET). Edit it and set a real shared secret before installing."
    exit 1
}
if ($config.web_password -eq "CHANGE_ME_FAMILY_PASSWORD") {
    Write-Error "config.json still has the default web_password (CHANGE_ME_FAMILY_PASSWORD). Set a real password for the web panel before installing."
    exit 1
}
try {
    [void][ipaddress]::Parse(($config.lan_subnet -split '/')[0])
} catch {
    Write-Error "config.json has an invalid lan_subnet: '$($config.lan_subnet)' (expected CIDR, e.g. 192.168.1.0/24)."
    exit 1
}

function Find-RealPython {
    # Returns the path to a real python.exe, or $null. Excludes the
    # Microsoft Store stub (WindowsApps), which is not a real Python install.
    $fromPath = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($fromPath -and $fromPath -notlike "*WindowsApps*" -and (Test-Path $fromPath)) {
        return $fromPath
    }
    # The py launcher is not affected by the PATH reset that happens after
    # UAC elevation, so try it before falling back to well-known locations.
    $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if ($py) {
        try {
            $resolved = & $py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
            if ($resolved -and $resolved -notlike "*WindowsApps*" -and (Test-Path $resolved)) {
                return $resolved
            }
        } catch {
        }
    }
    $bases = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        "C:\Python",
        "C:\Program Files\Python",
        "C:\Program Files (x86)\Python"
    )
    foreach ($base in $bases) {
        if ($base -and (Test-Path $base)) {
            $found = Get-ChildItem -Path $base -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($found) {
                return $found.FullName
            }
        }
    }
    return $null
}

function Install-Python {
    # Downloads and silently installs Python 3 machine-wide.
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $version = "3.12.10"
    $url = "https://www.python.org/ftp/python/$version/python-$version-$arch.exe"
    $installer = Join-Path $env:TEMP "python-$version-$arch.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Write-Host "Downloading Python $version ($arch)..."
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    } catch {
        Write-Error "Failed to download Python from $url : $_"
        return $null
    }
    Write-Host "Installing Python silently (machine-wide). This may take a minute..."
    $p = Start-Process -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_pip=1", "Include_test=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Error "Python installer failed with exit code $($p.ExitCode). Install Python manually from https://www.python.org/downloads/ and re-run this script."
        return $null
    }
    $installed = Join-Path "C:\Program Files\Python$($version.Split('.')[0..1] -join '')" "python.exe"
    if (Test-Path $installed) {
        return $installed
    }
    return (Find-RealPython)
}

$python = Find-RealPython
if (-not $python) {
    Write-Host "Python 3 not installed. Attempting to install it automatically..."
    $python = Install-Python
}
if (-not $python) {
    Write-Error "Python 3 not found. Install it from https://www.python.org/downloads/ and re-run this script."
    exit 1
}

$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) {
    Write-Error "pythonw.exe not found next to '$python'. Install Python 3 from https://www.python.org/downloads/ and re-run this script."
    exit 1
}

Write-Host "Python: $python"
Write-Host "Installing dependencies..."
& $python -m pip install -r (Join-Path $scriptDir "requirements.txt")
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

# The scheduled task's LastTaskResult only reflects the pythonw launcher's
# exit, which can be 0 even when the agent crashes a few seconds later (e.g.
# during firewall.ensure_rules). A real liveness check: wait for the HTTP
# port to start listening, then verify the agent is still alive.
$deadline = (Get-Date).AddSeconds(15)
$listening = $false
while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -State Listen -LocalPort $config.port -ErrorAction SilentlyContinue) {
        $listening = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ""
Write-Host "LastTaskResult: $($taskInfo.LastTaskResult)  (0 = pythonw started, anything else = the agent crashed on startup)"
if (-not $listening) {
    Write-Host "WARNING: The agent did NOT start cleanly (HTTP port $($config.port) is not listening). Read the log below for the reason:"
    Write-Host ""
    Write-Host "  Get-Content -Tail 50 `"$logPath`""
    Write-Host ""
    Write-Host "Or run the full diagnostics bundle and send the output to whoever maintains this:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$(Join-Path $scriptDir 'diagnose.ps1')`""
} else {
    Write-Host "Agent started successfully (HTTP port $($config.port) is listening)."
}

Write-Host ""
Write-Host "Done. The agent will start automatically at every logon (admin rights, no UAC prompt)."
Write-Host "Agent log file: $logPath"
Write-Host "Open http://<this-computer-ip>:$($config.port) from any device on the LAN to manage schedule, tasks and messages."
