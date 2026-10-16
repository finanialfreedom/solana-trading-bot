# One-shot progress report for the sweep program. Read-only: safe to run any time,
# even while the sweep is working. Prints each plan stage, whether it is done, and if
# it is the stage currently running, how far along its current arm is.
$ErrorActionPreference = 'Continue'
Set-Location 'D:\AI\solana-trading-bot'

$stateFile = 'reports\logs\program_state.txt'
$logFile   = 'reports\logs\program.log'
$doneFlag  = 'reports\logs\program_complete.flag'

# The 24-hour validation is a manual gate, so it is reported on its own line below the
# five automatic stages rather than inside $planOrder.
$approvalFile = 'reports\logs\hour24_approved.txt'
$hour24State  = 'reports\logs\hour24_state.txt'
$hour24Flag   = 'reports\logs\hour24_complete.flag'

$planOrder = @('slippage', 'threshold', 'protect2', 'ladder', 'final')
$done = @(Get-Content $stateFile -ErrorAction SilentlyContinue | Where-Object { $_ })

Write-Output ''
Write-Output ("SWEEP PROGRAM STATUS   {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Output ('-' * 72)

if (Test-Path $doneFlag) {
    Write-Output ("COMPLETE at {0}" -f (Get-Content $doneFlag))
} else {
    Write-Output 'IN PROGRESS'
}
Write-Output ''

# Which arm is running right now?
$arm = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*run_experiment*' } | Select-Object -First 1
$armLabel = ''
if ($arm) {
    $armLabel = ($arm.CommandLine -split 'run_experiment.py ')[1].Split(' ')[0]
    $started = $arm.CreationDate
    $mins = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
}

foreach ($stage in $planOrder) {
    $state = if ($done -contains $stage) { 'DONE   ' }
             elseif ($done -contains "$stage-ABANDONED") { 'FAILED ' }
             else { 'pending' }

    $out = "reports\logs\sweep_$stage.out"
    $detail = ''
    if (Test-Path $out) {
        $arms = Select-String -Path $out -Pattern '>>> (\S+)' -AllMatches |
            ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value }
        $finished = Select-String -Path $out -Pattern 'sweep finished' -Quiet
        $detail = "arms: $($arms -join ', ')"
        if ($finished) { $detail += '  [printed completion marker]' }
    }
    Write-Output ("  {0,-9} {1,-8} {2}" -f $stage, $state, $detail)
}

# The sixth step is not a stage: it never starts unless a human unlocks it.
$h24 = if (Test-Path $hour24Flag) { 'COMPLETE' }
       elseif (Test-Path $approvalFile) { 'APPROVED' }
       elseif (Test-Path $doneFlag) { 'WAITING ' }
       else { 'gated' }
$h24detail = ''
if (Test-Path $hour24State) {
    $arms24 = @(Get-Content $hour24State | Where-Object { $_ })
    if ($arms24) { $h24detail = "arms: $($arms24 -join ', ')" }
}
if (Test-Path $approvalFile) {
    $planned = Select-String -Path $approvalFile -Pattern '^arm\s*=\s*(\S+)' -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value }
    $secs = Select-String -Path $approvalFile -Pattern '^secs\s*=\s*(\d+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1
    if (-not $secs) { $secs = 86400 }
    $h24detail = "approved: $($planned -join ', ') @ ${secs}s each"
}
Write-Output ("  {0,-9} {1,-8} {2}" -f 'hour24', $h24, $h24detail)
if ($h24 -eq 'WAITING ') {
    Write-Output '            ^ manual gate: write reports\logs\hour24_approved.txt, then re-run with -RunHour24'
}

Write-Output ''
if ($armLabel) {
    Write-Output ("running now: {0}  ({1} min into a 30 min block)" -f $armLabel, $mins)
} else {
    Write-Output 'running now: (nothing)'
}

# Pooled results, if any block has completed.
$results = 'reports\logs\sweep_results.jsonl'
if (Test-Path $results) {
    $n = (Get-Content $results | Where-Object { $_.Trim() }).Count
    Write-Output ("completed blocks recorded: {0}" -f $n)
} else {
    Write-Output 'completed blocks recorded: 0'
}

Write-Output ''
Write-Output 'recent program.log:'
Get-Content $logFile -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { "  $_" }
Write-Output ''
