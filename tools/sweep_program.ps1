# Resumable test program. Safe to run repeatedly: completed stages are recorded in a
# state file and skipped, so a kill or crash costs at most the stage that was running.
#
# Each stage retries on its own, a stage is only marked done when the sweep prints its
# completion marker, and the caller (run_sweep_program.cmd) relaunches this script if it
# is ever killed. Between them the program survives Cursor closing, a crash, or an
# antivirus kill -- it just resumes where it stopped.
$ErrorActionPreference = 'Continue'
$root = 'D:\AI\solana-trading-bot'
Set-Location $root

$stateFile = Join-Path $root 'reports\logs\program_state.txt'
$logFile = Join-Path $root 'reports\logs\program.log'
$lockFile = Join-Path $root 'reports\logs\program.lock'
$doneFlag = Join-Path $root 'reports\logs\program_complete.flag'
$startFile = Join-Path $root 'reports\logs\program_started.txt'

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line
}

# Single-instance guard. This script is launched repeatedly by Task Scheduler, so a
# second copy must never start and fight the first over the same ports and log files.
if (Test-Path $lockFile) {
    $holder = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($holder -and (Get-Process -Id $holder -ErrorAction SilentlyContinue)) {
        exit 0
    }
}
Set-Content -Path $lockFile -Value $PID -NoNewline

# Fresh instance confirmed. If Norton's firewall is blocking the RPC or the WebSocket
# trade stream, everything below still "runs" but produces no trades -- a silent,
# expensive failure. Check first and leave a timestamped record.
& "$root\.venv\Scripts\python.exe" tools\check_connectivity.py --log 2>&1 |
    ForEach-Object { Log "net: $_" }

# Ordered: slippage decides go/no-go, so it runs first. Then what to buy, then how to
# sell, then the combined confirmation.
$plan = @(
    @{ Name = 'slippage';  Secs = 1800; Reps = 2 },
    @{ Name = 'threshold'; Secs = 1800; Reps = 2 },
    @{ Name = 'protect2';  Secs = 1800; Reps = 2 },
    @{ Name = 'ladder';    Secs = 1800; Reps = 2 },
    @{ Name = 'final';     Secs = 1800; Reps = 2 }
)

if (Test-Path $doneFlag) { Log 'already complete; nothing to do'; exit 0 }
if (-not (Test-Path $stateFile)) { New-Item -ItemType File -Path $stateFile -Force | Out-Null }

# Record when this program's run began, once. The final scoreboard uses it to ignore
# experiments from earlier runs, which were scored with the old, flattering execution
# math and are not comparable.
if (-not (Test-Path $startFile)) {
    Set-Content -Path $startFile -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
}
$programStart = (Get-Content $startFile -ErrorAction SilentlyContinue | Select-Object -First 1)

$done = @(Get-Content $stateFile -ErrorAction SilentlyContinue | Where-Object { $_ })

Log ("resume: already done -> " + ($(if ($done) { $done -join ', ' } else { 'none' })))

foreach ($step in $plan) {
    $name = $step.Name
    if ($done -contains $name) { Log "skip $name (already done)"; continue }

    $out = Join-Path $root "reports\logs\sweep_$name.out"
    $err = Join-Path $root "reports\logs\sweep_$name.err"
    $ok = $false

    for ($attempt = 1; $attempt -le 6 -and -not $ok; $attempt++) {
        Log "start $name attempt $attempt ($($step.Reps) x $($step.Secs)s)"
        & "$root\.venv\Scripts\python.exe" -u tools\interleaved_sweep.py $step.Secs $step.Reps $name > $out 2> $err
        $code = $LASTEXITCODE
        $finished = Select-String -Path $out -Pattern 'sweep finished' -Quiet -ErrorAction SilentlyContinue
        if ($finished) {
            $ok = $true
            Log "$name DONE (exit $code)"
        } else {
            Log "$name attempt $attempt unfinished (exit $code); retry in 30s"
            Start-Sleep -Seconds 30
        }
    }

    if ($ok) {
        Add-Content -Path $stateFile -Value $name
    } else {
        # Do not block the rest of the night on one stage; record it and move on.
        Log "WARNING: $name abandoned after 6 attempts"
        Add-Content -Path $stateFile -Value "$name-ABANDONED"
    }
}

Set-Content -Path $doneFlag -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Remove-Item $lockFile -ErrorAction SilentlyContinue
Log "PROGRAM COMPLETE"

# The two numbers that decide go-live: the best success rate reached, and the most
# aggressive take-profit setting that still finished ahead at the fixed 3% stop loss.
Log "writing final scoreboard"
& "$root\.venv\Scripts\python.exe" tools\final_report.py "--since=$programStart" > (Join-Path $root 'reports\logs\final_report.out') 2>&1
Log "final scoreboard -> reports\final_report.txt"
