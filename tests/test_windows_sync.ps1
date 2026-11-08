#requires -Version 7.0
$ErrorActionPreference = 'Stop'
$syncScript = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\sync-windows.ps1'
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('video-timeline-sync-test-' + [Guid]::NewGuid().ToString('N'))
$origin = Join-Path $fixture 'origin.git'
$seed = Join-Path $fixture 'seed'
$checkout = Join-Path $fixture 'checkout'
$state = Join-Path $fixture 'state'
$pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
$fixtureGit = (Get-Command git -CommandType Application | Select-Object -First 1).Source
$passed = [Collections.Generic.List[string]]::new()

function Git {
    param([string]$Path, [string[]]$GitArgs)
    $output = & $fixtureGit -C $Path @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('Fixture Git failed: ' + ($output -join "`n")) }
    return ($output -join "`n").Trim()
}
function Assert {
    param([bool]$Condition, [string]$Name)
    if (-not $Condition) { throw ('FAIL: ' + $Name + '; fixture retained at ' + $fixture) }
    $passed.Add($Name)
}
function Sync {
    param([string[]]$Extra = @(), [int]$ExpectedExit = 0)
    $output = & $pwsh -NoProfile -NonInteractive -File $syncScript -RepoPath $checkout -StateDir $state -AllowLocalTestRemote -ExpectedRepository $origin @Extra
    Assert ($LASTEXITCODE -eq $ExpectedExit) 'sync process exit'
    return ($output -join "`n" | ConvertFrom-Json)
}
function Publish {
    param([string]$Version)
    Set-Content -LiteralPath (Join-Path $seed 'code.txt') -Value $Version -Encoding utf8
    [void](Git $seed @('add', 'code.txt'))
    [void](Git $seed @('commit', '-m', $Version))
    [void](Git $seed @('push', 'origin', 'main'))
    return Git $seed @('rev-parse', 'HEAD')
}

[void][IO.Directory]::CreateDirectory($fixture)
[void][IO.Directory]::CreateDirectory($seed)
[void](Git $fixture @('init', '--bare', '--initial-branch=main', $origin))
[void](Git $seed @('init', '--initial-branch=main'))
[void](Git $seed @('config', 'user.name', 'Offline Sync Fixture'))
[void](Git $seed @('config', 'user.email', 'offline-fixture@example.invalid'))
[void](Git $seed @('remote', 'add', 'origin', $origin))
Set-Content -LiteralPath (Join-Path $seed '.gitignore') -Value 'runtime-secret.txt' -Encoding utf8
[void](Git $seed @('add', '.gitignore'))
$first = Publish 'v1'
[void](Git $fixture @('clone', $origin, $checkout))
[void](Git $checkout @('config', 'user.name', 'Offline Sync Fixture'))
[void](Git $checkout @('config', 'user.email', 'offline-fixture@example.invalid'))
$next = Publish 'v2'
Set-Content -LiteralPath (Join-Path $checkout 'code.txt') -Value 'local dirty' -Encoding utf8
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.fetched -and $result.before_sha -eq $first -and $result.target_sha -eq $next) 'dirty checkout fetches without updating HEAD'
Assert ((Get-Content -LiteralPath (Join-Path $checkout 'code.txt') -Raw).Trim() -eq 'local dirty') 'dirty contents preserved'
Set-Content -LiteralPath (Join-Path $checkout 'code.txt') -Value 'v1' -Encoding utf8
Set-Content -LiteralPath (Join-Path $checkout 'untracked.txt') -Value 'keep me' -Encoding utf8
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.after_sha -eq $first) 'untracked content blocks automatic update'
Remove-Item -LiteralPath (Join-Path $checkout 'untracked.txt')
Set-Content -LiteralPath (Join-Path $checkout 'code.txt') -Value 'staged dirty' -Encoding utf8
[void](Git $checkout @('add', 'code.txt'))
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.after_sha -eq $first) 'staged content blocks automatic update'
[void](Git $checkout @('restore', '--staged', '--worktree', 'code.txt'))
$result = Sync -Extra @('-CheckOnly')
Assert ($result.status -eq 'WOULD_FAST_FORWARD' -and $result.after_sha -eq $first) 'check-only fetches but never updates checkout'
Set-Content -LiteralPath (Join-Path $checkout 'runtime-secret.txt') -Value 'runtime data' -Encoding utf8
$result = Sync
Assert ($result.status -eq 'UPDATED' -and $result.updated -and $result.after_sha -eq $next) 'clean main fast-forwards to fetched SHA'
Assert ((Get-Content -LiteralPath (Join-Path $checkout 'runtime-secret.txt') -Raw).Trim() -eq 'runtime data') 'ignored runtime data is preserved'
$result = Sync
Assert ($result.status -eq 'UP_TO_DATE' -and -not $result.updated) 'repeated sync is idempotent'
[void](Git $checkout @('switch', '-c', 'feature'))
$third = Publish 'v3'
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.target_sha -eq $third -and $result.after_sha -eq $next) 'feature branch fetches but never updates checkout'
[void](Git $checkout @('switch', 'main'))
$marker = Join-Path $checkout '.git\MERGE_HEAD'
Set-Content -LiteralPath $marker -Value $third -Encoding ascii
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.reason -match 'progress' -and $result.after_sha -eq $next) 'in-progress Git operation blocks automatic update'
Remove-Item -LiteralPath $marker
$result = Sync
Assert ($result.status -eq 'UPDATED' -and $result.after_sha -eq $third) 'clean checkout updates after Git operation completes'
Set-Content -LiteralPath (Join-Path $seed 'runtime-secret.txt') -Value 'incoming tracked fixture' -Encoding utf8
[void](Git $seed @('add', '--force', 'runtime-secret.txt'))
$fourth = Publish 'v4'
$result = Sync -ExpectedExit 1
Assert ($result.status -eq 'ERROR' -and $result.after_sha -eq $third -and $result.target_sha -eq $fourth) 'incoming tracked file cannot overwrite ignored runtime data'
Assert ((Get-Content -LiteralPath (Join-Path $checkout 'runtime-secret.txt') -Raw).Trim() -eq 'runtime data') 'ignored data survives rejected merge'
Set-Content -LiteralPath (Join-Path $checkout 'local.txt') -Value 'local commit' -Encoding utf8
[void](Git $checkout @('add', 'local.txt'))
[void](Git $checkout @('commit', '-m', 'local divergent commit'))
$localSha = Git $checkout @('rev-parse', 'HEAD')
$result = Sync
Assert ($result.status -eq 'FETCH_ONLY' -and $result.after_sha -eq $localSha -and $result.reason -match 'diverged') 'diverged main is preserved'
$heldLock = [IO.File]::Open((Join-Path $state 'sync.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $result = Sync
    Assert ($result.status -eq 'LOCKED' -and -not $result.fetched) 'concurrent invocation respects lock'
} finally { $heldLock.Dispose() }
[void](Git $checkout @('remote', 'set-url', 'origin', 'https://github.com/example/wrong.git'))
$result = Sync -ExpectedExit 1
Assert ($result.status -eq 'ERROR' -and -not $result.fetched -and $result.after_sha -eq $null) 'wrong remote is rejected before fetch'
$receipts = @(Get-ChildItem -LiteralPath (Join-Path $state 'receipts') -Filter '*.json')
Assert ($receipts.Count -ge 12) 'every run leaves an external JSON receipt'
[pscustomobject]@{ status = 'PASS'; checks = $passed.Count; fixture_path = $fixture; check_names = $passed.ToArray() } | ConvertTo-Json -Depth 3
