#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RepoPath,
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'VideoTimelinePipeline\git-sync'),
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [ValidateSet('SSH', 'Configured')][string]$Transport = 'SSH',
    [ValidateRange(5, 600)][int]$GitTimeoutSeconds = 90,
    [switch]$CheckOnly,
    [string]$ExpectedRepository = 'Reese-max/video-timeline-pipeline',
    [switch]$AllowLocalTestRemote
)

$ErrorActionPreference = 'Stop'
$RepoPath = [IO.Path]::GetFullPath($RepoPath)
$StateDir = [IO.Path]::GetFullPath($StateDir)
if ($StateDir.Equals($RepoPath, [StringComparison]::OrdinalIgnoreCase) -or
    $StateDir.StartsWith($RepoPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'StateDir must be outside the repository.'
}
[void][IO.Directory]::CreateDirectory((Join-Path $StateDir 'receipts'))
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N')
$receipt = [ordered]@{
    schema_version = 1; run_id = $runId; timestamp_utc = [DateTime]::UtcNow.ToString('o')
    repo_path = $RepoPath; remote = $Remote; target_branch = $Branch; transport = $Transport
    test_fixture = [bool]$AllowLocalTestRemote; check_only = [bool]$CheckOnly
    branch = $null; before_sha = $null; target_sha = $null; after_sha = $null
    fetched = $false; updated = $false; status = 'ERROR'; reason = $null; git_exit_code = $null
}
$lock = $null
$exitCode = 0

function Invoke-Git {
    param([string[]]$GitArgs, [int[]]$AllowedExitCodes = @(0), [switch]$UseTransport)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $gitPath
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($arg in @('-c', 'credential.interactive=false', '-c', 'maintenance.auto=0', '-c', 'gc.auto=0', '-c', ('core.hooksPath=' + (Join-Path $StateDir 'disabled-hooks')), '-C', $RepoPath)) {
        $start.ArgumentList.Add($arg)
    }
    if ($UseTransport -and -not $AllowLocalTestRemote -and $Transport -eq 'SSH') {
        $start.ArgumentList.Add('-c')
        $start.ArgumentList.Add('url.git@github.com:.insteadOf=https://github.com/')
    }
    foreach ($arg in $GitArgs) { $start.ArgumentList.Add($arg) }
    $start.Environment['GIT_TERMINAL_PROMPT'] = '0'
    $start.Environment['GCM_INTERACTIVE'] = 'Never'
    $start.Environment['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8'
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($GitTimeoutSeconds * 1000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw 'Git operation timed out; inspect network or credentials.'
        }
        $output = $stdout.GetAwaiter().GetResult().TrimEnd()
        [void]$stderr.GetAwaiter().GetResult()
        $receipt.git_exit_code = $process.ExitCode
        if ($process.ExitCode -notin $AllowedExitCodes) {
            # Do not persist Git stderr: URLs and helpers can contain credentials.
            throw ('Git ' + $GitArgs[0] + ' failed with exit code ' + $process.ExitCode + '.')
        }
        return [pscustomobject]@{ Text = $output; ExitCode = $process.ExitCode }
    } finally { $process.Dispose() }
}

try {
    try {
        $lock = [IO.File]::Open((Join-Path $StateDir 'sync.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        $receipt.status = 'LOCKED'
        $receipt.reason = 'Another sync process holds the state-directory lock.'
    }
    if ($lock) {
        $gitPath = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        if ($Remote -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw 'Invalid Git remote name.' }
        [void](Invoke-Git -GitArgs @('check-ref-format', '--branch', $Branch))
        $top = (Invoke-Git -GitArgs @('rev-parse', '--show-toplevel')).Text
        if (-not ([IO.Path]::GetFullPath($top).Equals($RepoPath, [StringComparison]::OrdinalIgnoreCase))) {
            throw 'RepoPath must be the Git working-tree root.'
        }
        $url = (Invoke-Git -GitArgs @('remote', 'get-url', '--all', $Remote)).Text
        if ($AllowLocalTestRemote) {
            if (-not [IO.Path]::IsPathFullyQualified($url) -or $url.StartsWith('\\') -or $url -ne $ExpectedRepository) {
                throw 'Test remote must exactly match the explicit local fixture path.'
            }
        } elseif ($ExpectedRepository -ne 'Reese-max/video-timeline-pipeline' -or
            $url -notmatch '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)Reese-max/video-timeline-pipeline(?:\.git)?/?$') {
            throw 'Remote does not match Reese-max/video-timeline-pipeline.'
        }
        $receipt.before_sha = (Invoke-Git -GitArgs @('rev-parse', 'HEAD')).Text
        $receipt.after_sha = $receipt.before_sha
        $receipt.branch = (Invoke-Git -GitArgs @('symbolic-ref', '--quiet', '--short', 'HEAD') -AllowedExitCodes @(0, 1)).Text
        [void](Invoke-Git -GitArgs @('fetch', '--no-tags', $Remote, ('+refs/heads/' + $Branch + ':refs/remotes/' + $Remote + '/' + $Branch)) -UseTransport)
        $receipt.fetched = $true
        $target = 'refs/remotes/' + $Remote + '/' + $Branch
        $receipt.target_sha = (Invoke-Git -GitArgs @('rev-parse', '--verify', $target)).Text
        $inProgress = $false
        foreach ($marker in @('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG', 'index.lock')) {
            $markerPath = (Invoke-Git -GitArgs @('rev-parse', '--git-path', $marker)).Text
            if (-not [IO.Path]::IsPathFullyQualified($markerPath)) { $markerPath = Join-Path $RepoPath $markerPath }
            if (Test-Path -LiteralPath $markerPath) { $inProgress = $true; break }
        }
        $dirty = (Invoke-Git -GitArgs @('status', '--porcelain=v1', '--untracked-files=all')).Text
        if ($inProgress) {
            $receipt.status = 'FETCH_ONLY'; $receipt.reason = 'Git operation or index lock is in progress.'
        } elseif ($dirty) {
            $receipt.status = 'FETCH_ONLY'; $receipt.reason = 'Tracked, staged, or untracked changes require review.'
        } elseif ($receipt.branch -ne $Branch) {
            $receipt.status = 'FETCH_ONLY'; $receipt.reason = 'Checkout is not on the formal branch.'
        } elseif ($receipt.before_sha -eq $receipt.target_sha) {
            $receipt.status = 'UP_TO_DATE'
        } elseif ((Invoke-Git -GitArgs @('merge-base', '--is-ancestor', 'HEAD', $receipt.target_sha) -AllowedExitCodes @(0, 1)).ExitCode -ne 0) {
            $receipt.status = 'FETCH_ONLY'; $receipt.reason = 'Local branch is ahead or diverged; automatic merge refused.'
        } elseif ($CheckOnly) {
            $receipt.status = 'WOULD_FAST_FORWARD'
        } else {
            # Recheck immediately before checkout changes; Git handles its own index lock.
            $currentBranch = (Invoke-Git -GitArgs @('symbolic-ref', '--quiet', '--short', 'HEAD') -AllowedExitCodes @(0, 1)).Text
            $currentHead = (Invoke-Git -GitArgs @('rev-parse', 'HEAD')).Text
            $currentDirty = (Invoke-Git -GitArgs @('status', '--porcelain=v1', '--untracked-files=all')).Text
            if ($currentBranch -ne $Branch -or $currentHead -ne $receipt.before_sha -or $currentDirty) {
                $receipt.status = 'FETCH_ONLY'; $receipt.reason = 'Checkout changed during sync; retry on the next schedule.'
            } else {
                [void](Invoke-Git -GitArgs @('merge', '--ff-only', '--no-edit', '--no-autostash', '--no-overwrite-ignore', $receipt.target_sha))
                $receipt.after_sha = (Invoke-Git -GitArgs @('rev-parse', 'HEAD')).Text
                if ($receipt.after_sha -ne $receipt.target_sha) { throw 'Post-update HEAD does not match fetched target.' }
                $receipt.updated = $true
                $receipt.status = 'UPDATED'
            }
        }
    }
} catch {
    $receipt.status = 'ERROR'
    $receipt.reason = $_.Exception.Message
    $exitCode = 1
} finally {
    $json = $receipt | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $StateDir ('receipts/' + $runId + '.json')), $json, [Text.UTF8Encoding]::new($false))
    if ($lock) {
        $latestTemp = Join-Path $StateDir ($runId + '.tmp')
        [IO.File]::WriteAllText($latestTemp, $json, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($latestTemp, (Join-Path $StateDir 'latest.json'), $true)
        $lock.Dispose()
    }
    Write-Output $json
}
exit $exitCode
