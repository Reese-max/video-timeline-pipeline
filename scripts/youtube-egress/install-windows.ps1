# Install (or remove) the YouTube egress proxy on a Windows home PC. Run elevated on that PC.
# ASCII only so Windows PowerShell 5.1 reads it correctly. See docs/gitops.md (A1, YouTube egress).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-windows.ps1 -ListenAddress 100.74.111.42 -AllowClient 100.82.40.33
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-windows.ps1 -Uninstall
param(
    [string]$ListenAddress,
    [int]$Port = 18443,
    [string[]]$AllowClient,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'youtube-egress'),
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$TaskName = 'YouTube egress proxy (A1)'
$RuleName = 'YouTube egress proxy (A1 only)'
$Script = Join-Path $InstallDir 'egress_proxy.py'

function Stop-Proxy {
    # Unregistering a task does not stop its process: stop the task, then any pythonw still running this exact script.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Script, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Output "stopped pid $($_.ProcessId)"
        }
}

function Assert-TailscaleIPv4([string]$Value) {
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($Value, [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork' -or $ip.ToString() -ne $Value) {
        throw "not a plain IPv4 address: $Value"
    }
    $b = $ip.GetAddressBytes()
    if ($b[0] -ne 100 -or $b[1] -lt 64 -or $b[1] -gt 127) { throw "not a Tailscale (100.64.0.0/10) address: $Value" }
}

if ($Uninstall) {
    Stop-Proxy
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Output "removed task and firewall rule; files left in $InstallDir"
    return
}
if (-not $ListenAddress -or -not $AllowClient) { throw 'ListenAddress and AllowClient are required' }
Assert-TailscaleIPv4 $ListenAddress
$AllowClient | ForEach-Object { Assert-TailscaleIPv4 $_ }

$python = (Get-Command python -ErrorAction Stop).Source
if ($python -like '*\WindowsApps\*') { throw "python resolves to the Store alias ($python); install python.org Python" }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }
Stop-Proxy

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'egress_proxy.py') $Script
$log = Join-Path $InstallDir 'egress.log'
$clientArgs = ($AllowClient | ForEach-Object { "--allow-client $_" }) -join ' '
$arguments = "`"$Script`" --listen ${ListenAddress}:$Port $clientArgs --log `"$log`""

# Starts at boot without a logon session and without a stored password (S4U); restarts if it dies.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
# Not $env:USERDOMAIN: over SSH it can read WORKGROUP instead of the machine name of a local account.
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $ListenAddress -LocalPort $Port -RemoteAddress $AllowClient -Profile Any | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object LocalAddress -eq $ListenAddress
Write-Output ("task: " + (Get-ScheduledTask -TaskName $TaskName).State + "; listening: " + [bool]$listening + "; log: $log")
