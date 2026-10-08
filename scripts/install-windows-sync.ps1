#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$RepoPath,
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'VideoTimelinePipeline\git-sync'),
    [ValidateSet('Interactive', 'S4U')][string]$LogonType = 'Interactive',
    [ValidateSet('SSH', 'Configured')][string]$Transport = 'SSH',
    [string]$TaskName = 'VideoTimelinePipeline-GitSync'
)

$ErrorActionPreference = 'Stop'
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
$StateDir = [IO.Path]::GetFullPath($StateDir)
$scriptPath = Join-Path $PSScriptRoot 'sync-windows.ps1'
$pwsh = (Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
foreach ($value in @($RepoPath, $StateDir, $scriptPath, $TaskName)) {
    if ($value.Contains('"')) { throw 'Paths and task name must not contain a double quote.' }
}
if ($StateDir.Equals($RepoPath, [StringComparison]::OrdinalIgnoreCase) -or
    $StateDir.StartsWith($RepoPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'StateDir must be outside the repository.'
}
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -NonInteractive -File "' + $scriptPath + '" -RepoPath "' + $RepoPath + '" -StateDir "' + $StateDir + '" -Transport ' + $Transport
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
$triggers = @(
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
    New-ScheduledTaskTrigger -AtLogOn -User $account
)
if ($LogonType -eq 'S4U') { $triggers += New-ScheduledTaskTrigger -AtStartup }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType $LogonType -RunLevel Limited
$description = 'Fetch GitHub main every 15 minutes; fast-forward only clean main; receipts outside checkout. ' + $(if ($LogonType -eq 'S4U') { 'S4U requires successful credential smoke test; no password is stored.' } else { 'Interactive requires this user to be logged in; startup catches up at next logon.' })
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $description
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pwsh -or $existing.Actions[0].Arguments -ne $arguments)) {
    throw 'An existing task with this name has a different command; preserve and inspect it before replacing.'
}
if ($PSCmdlet.ShouldProcess($TaskName, 'Register safe 15-minute Git sync for ' + $account)) {
    if ($existing) {
        $backupDir = Join-Path $StateDir 'task-backups'
        [void][IO.Directory]::CreateDirectory($backupDir)
        $backup = Join-Path $backupDir ($TaskName + '.xml.bak-' + (Get-Date -Format yyyyMMdd))
        if (Test-Path -LiteralPath $backup) { $backup += '-' + (Get-Date -Format HHmmss) + '-' + [Guid]::NewGuid().ToString('N') }
        Export-ScheduledTask -TaskName $TaskName -TaskPath '\' | Set-Content -LiteralPath $backup -Encoding utf8
    }
    [void](Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $task -Force -ErrorAction Stop)
    $readback = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    if (-not $readback -or $readback.Actions[0].Execute -ne $pwsh -or $readback.Actions[0].Arguments -ne $arguments) {
        throw 'Task registration could not be verified by readback.'
    }
    [pscustomobject]@{
        task_name = $readback.TaskName; account = $readback.Principal.UserId; logon_type = [string]$readback.Principal.LogonType
        state = [string]$readback.State; repo_path = $RepoPath; state_dir = $StateDir
        interval_minutes = 15; transport = $Transport; password_stored = $false
        requires_logged_in_user = ($LogonType -eq 'Interactive'); credentials_verified = $false
    } | ConvertTo-Json
}
