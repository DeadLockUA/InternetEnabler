# InternetEnabler client diagnostics bundle.
# Run this on the TARGET machine (where the agent should be running).
# It prints one paste-able block of diagnostics - send the full output to
# whoever is helping you troubleshoot.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File diagnose.ps1
#   (no admin rights needed; read-only checks only)

$ErrorActionPreference = "Continue"

function Section($title) {
    Write-Output ""
    Write-Output "==================== $title ===================="
}

$task = Get-ScheduledTask -TaskName "InternetEnablerAgent" -ErrorAction SilentlyContinue

# --- Scheduled task state ------------------------------------------------
Section "SCHEDULED TASK"
if (-not $task) {
    Write-Output "TASK NOT FOUND: InternetEnablerAgent is not registered."
} else {
    Write-Output "State: $($task.State)"
    $info = Get-ScheduledTaskInfo -TaskName "InternetEnablerAgent"
    Write-Output "LastRunTime    : $($info.LastRunTime)"
    Write-Output "LastTaskResult : $($info.LastTaskResult)"
    Write-Output "NextRunTime    : $($info.NextRunTime)"
    Write-Output "NumberOfMissedRuns: $($info.NumberOfMissedRuns)"

    Section "SCHEDULED TASK ACTION"
    foreach ($a in $task.Actions) {
        Write-Output "Execute          : $($a.Execute)"
        Write-Output "Arguments        : $($a.Arguments)"
        Write-Output "WorkingDirectory : $($a.WorkingDirectory)"
    }

    Section "SCHEDULED TASK PRINCIPAL"
    Write-Output "UserId    : $($task.Principal.UserId)"
    Write-Output "LogonType : $($task.Principal.LogonType)"
    Write-Output "RunLevel  : $($task.Principal.RunLevel)"
}

# --- Agent install directory (from the task action) -----------------------
Section "AGENT DIRECTORY"
if ($task -and $task.Actions.Count -gt 0) {
    $workDir = $task.Actions[0].WorkingDirectory
    if ($workDir) { Set-Location $workDir }
    Write-Output "Working directory set to: $(Get-Location)"
} else {
    Write-Output "Using current directory: $(Get-Location)"
}
Write-Output "Directory exists: $(Test-Path (Get-Location))"

# --- Config.json ----------------------------------------------------------
Section "CONFIG.JSON"
$configPath = Join-Path (Get-Location) "config.json"
if (-not (Test-Path $configPath)) {
    Write-Output "MISSING: config.json not found at $configPath"
} else {
    try {
        $config = Get-Content $configPath -Raw | ConvertFrom-Json
        Write-Output "Valid JSON: yes"
        Write-Output "lan_subnet    : $($config.lan_subnet)"
        Write-Output "port          : $($config.port)"
        Write-Output "token set     : $(if ($config.token -and $config.token -ne 'CHANGE_ME_SHARED_SECRET') { 'yes' } else { 'NO - still default or empty!' })"
        Write-Output "web_password  : $(if ($config.web_password -and $config.web_password -ne 'CHANGE_ME_FAMILY_PASSWORD') { 'set' } else { 'NOT SET or still default!' })"
        Write-Output "reminder_minutes: $($config.reminder_minutes)"
    } catch {
        Write-Output "INVALID JSON: cannot parse config.json - $($_.Exception.Message)"
    }
}

# --- Python + dependencies ------------------------------------------------
Section "PYTHON"
$exe = $null
if ($task -and $task.Actions.Count -gt 0) {
    $exe = $task.Actions[0].Execute
}
$pythonExe = $null
if ($exe) {
    Write-Output "Task launcher : $exe"
    Write-Output "Launcher exists: $(Test-Path $exe)"
    # Derive the directory to locate the interpreter next to pythonw.exe
    $exeDir = Split-Path -Parent $exe
    $candidate = Join-Path $exeDir "python.exe"
    Write-Output "python.exe next to launcher exists: $(Test-Path $candidate)"
    if (Test-Path $candidate) { $pythonExe = $candidate }
}
if (-not $pythonExe) {
    # No task action executable, or python.exe isn't next to it (e.g. a
    # venv/embedded layout) - fall back to whatever python.exe is on PATH.
    $pythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    Write-Output "Falling back to PATH python: $(if ($pythonExe) { $pythonExe } else { 'not found' })"
}
if ($pythonExe) {
    $ver = & $pythonExe -c "import sys; print(sys.version.split()[0])" 2>$null
    Write-Output "Python version : $ver"
}

Section "DEPENDENCIES"
if ($pythonExe -and (Test-Path $pythonExe)) {
    Write-Output "Checking packages for: $pythonExe"
    & $pythonExe -c "import importlib.util as u; print('pystray:', 'OK' if u.find_spec('pystray') else 'MISSING'); print('Pillow :', 'OK' if u.find_spec('PIL') else 'MISSING')" 2>&1
} else {
    Write-Output "Could not locate python.exe to check dependencies."
}

# --- agent.log ------------------------------------------------------------
Section "AGENT.LOG (last 100 lines)"
$logPath = Join-Path (Get-Location) "agent.log"
if (-not (Test-Path $logPath)) {
    Write-Output "NO agent.log FOUND at $logPath"
    Write-Output "(If the agent crashed before logging was set up, this file may not exist - that itself is useful info.)"
} else {
    Write-Output "Log file: $logPath"
    Write-Output "Size    : $((Get-Item $logPath).Length) bytes"
    Get-Content $logPath -Tail 100
}

# --- Runtime checks ---------------------------------------------------------
Section "RUNNING PROCESS"
$procs = Get-Process -Name "pythonw" -ErrorAction SilentlyContinue
if ($procs) {
    Write-Output "pythonw processes:"
    $procs | ForEach-Object {
        Write-Output "  PID $($_.Id) - started $($_.StartTime) - $($_.Path)"
    }
} else {
    Write-Output "NO pythonw process is running - the agent is NOT currently running."
}

Section "PORT LISTENING"
$configPath = Join-Path (Get-Location) "config.json"
$port = $null
if (Test-Path $configPath) {
    try {
        $port = (Get-Content $configPath -Raw | ConvertFrom-Json).port
    } catch {}
}
if ($port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) {
        $pids = ($listener | ForEach-Object { $_.OwningProcess }) -join ", "
        Write-Output "Port $port is LISTENING (PID $pids)"
    } else {
        Write-Output "Port $port is NOT listening - the HTTP server is not up."
    }
} else {
    Write-Output "Could not determine expected port from config.json."
}

Section "END OF DIAGNOSTICS"
Write-Output "Copy everything above this line and paste it into the chat."