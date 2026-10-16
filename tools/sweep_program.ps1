# Resumable test program. Safe to run repeatedly: completed stages are recorded in a
# state file and skipped, so a kill or crash costs at most the stage that was running.
#
# Each stage retries on its own, a stage is only marked done when the sweep prints its
# completion marker, and the caller (run_sweep_program.cmd) relaunches this script if it
# is ever killed. Between them the program survives Cursor closing, a crash, or an
# antivirus kill -- it just resumes where it stopped.
#
# The five stages in $plan are automatic. The 24-hour validation that follows them is
# NOT: it sits behind a manual gate and only runs when a human passes -RunHour24 after
# reading the scoreboard. See the "24-HOUR VALIDATION" block below.
param(
    [switch]$RunHour24
)

$ErrorActionPreference = 'Continue'
$root = 'D:\AI\solana-trading-bot'
Set-Location $root

$stateFile = Join-Path $root 'reports\logs\program_state.txt'
$logFile = Join-Path $root 'reports\logs\program.log'
$lockFile = Join-Path $root 'reports\logs\program.lock'
$doneFlag = Join-Path $root 'reports\logs\program_complete.flag'
$startFile = Join-Path $root 'reports\logs\program_started.txt'

# The manual 24-hour gate. All three live beside the rest of the program state.
$approvalFile = Join-Path $root 'reports\logs\hour24_approved.txt'
$hour24State = Join-Path $root 'reports\logs\hour24_state.txt'
$hour24Flag = Join-Path $root 'reports\logs\hour24_complete.flag'

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line
}

function Clear-StrayBots {
    # An experiment's bot is launched as `python -c "import asyncio, bot ..."`, so it
    # matches none of the sweep script paths. If a stage is force-killed, that grandchild
    # can outlive the sweep and keep trading, writing to the same reports\*.csv as the arm
    # we are about to start -- two bots feeding one trade log corrupts both. Clear them
    # before every attempt; a bot we own is started fresh right after.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'import asyncio, bot' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Get-ApprovedArms {
    # Parse the manual approval file that unlocks the 24-hour validation. One directive
    # per line, human-editable:
    #
    #   secs=86400
    #   arm=<label>  KEY=value  KEY=value
    #
    # Returns $null when the file is missing or names no arms, so the caller refuses to
    # start rather than launching an empty sweep.
    if (-not (Test-Path $approvalFile)) { return $null }

    $arms = @()
    $secs = 86400
    foreach ($raw in (Get-Content $approvalFile -ErrorAction SilentlyContinue)) {
        $line = ($raw -replace '#.*$', '').Trim()
        if (-not $line) { continue }

        if ($line -match '^secs\s*=\s*(\d+)$') { $secs = [int]$Matches[1]; continue }
        if ($line -notmatch '^arm\s*=\s*(\S+)\s*(.*)$') { continue }

        $label = $Matches[1]
        $rest = $Matches[2].Trim()
        $overrides = @{}
        if ($rest) {
            foreach ($pair in ($rest -split '\s+')) {
                if ($pair -match '^([A-Za-z0-9_]+)\s*=\s*(.+)$') {
                    $overrides[$Matches[1]] = $Matches[2]
                }
            }
        }
        $arms += @{ Label = $label; Overrides = $overrides }
    }

    if (-not $arms) { return $null }
    return @{ Arms = $arms; Secs = $secs }
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

# ============================================================================
# 24-HOUR VALIDATION -- MANUAL GATE. This block never runs on its own.
# ============================================================================
# Task Scheduler launches this script with no arguments, so $RunHour24 is false and the
# run falls through to the stage plan below. After the last stage and the scoreboard the
# program stops; it does NOT begin the 24-hour run.
#
# The long run happens only when a human re-invokes this script with -RunHour24, after
# writing the approval file that names the configuration chosen from the finished
# results:
#
#   reports\logs\hour24_approved.txt
#       secs=86400
#       arm=best-entry2   MIN_CONFIRM_BUY_SOL=2
#       arm=best-stack    MIN_CONFIRM_BUY_SOL=2  MIN_CONFIRM_NET_SOL=1
#
# `secs` is the duration of EACH arm, so N arms cost N x secs of wall clock. For a true
# 24-hour validation approve one arm and leave secs at 86400.
#
# The gate exists because a 30-minute result may change what deserves a full day: the
# configuration that wins a short block is not always the one worth 24 hours, and the
# slippage stage showed the edge is thin enough that the choice matters. Nothing here
# starts until hani says so after reading reports\final_report.txt.
if ($RunHour24) {
    if (-not (Test-Path $approvalFile)) {
        Log '24h gate: -RunHour24 given but no approval file; refusing to start'
        Write-Output "No approval file at $approvalFile"
        Write-Output 'Write it first -- see the 24-HOUR VALIDATION comment block in this script.'
        Remove-Item $lockFile -ErrorAction SilentlyContinue
        exit 2
    }

    $approved = Get-ApprovedArms
    if (-not $approved) {
        Log '24h gate: approval file present but names no arms; refusing to start'
        Write-Output 'The approval file has no "arm=" lines. Nothing to run.'
        Remove-Item $lockFile -ErrorAction SilentlyContinue
        exit 2
    }

    Log ("24h gate APPROVED: {0} arm(s) x {1}s each" -f $approved.Arms.Count, $approved.Secs)
    $done24 = @(Get-Content $hour24State -ErrorAction SilentlyContinue | Where-Object { $_ })

    foreach ($arm in $approved.Arms) {
        if ($done24 -contains $arm.Label) { Log "24h skip $($arm.Label) (already done)"; continue }

        $out = Join-Path $root "reports\logs\hour24_$($arm.Label).out"
        $err = Join-Path $root "reports\logs\hour24_$($arm.Label).err"
        $ok = $false

        # Fewer retries than a stage: a full-day arm is expensive, and three failures in a
        # row means something another attempt will not fix.
        for ($attempt = 1; $attempt -le 3 -and -not $ok; $attempt++) {
            Log "24h start $($arm.Label) attempt $attempt ($($approved.Secs)s)"
            Clear-StrayBots
            # MAX_TRADES is raised far above the stage value: a stage covers 30 minutes,
            # this covers a day, and the session cap would otherwise stop trading early.
            $armArgs = @('tools\run_experiment.py', $arm.Label, "$($approved.Secs)", 'MAX_TRADES=100000')
            foreach ($k in $arm.Overrides.Keys) { $armArgs += "$k=$($arm.Overrides[$k])" }
            & "$root\.venv\Scripts\python.exe" -u $armArgs > $out 2> $err
            $code = $LASTEXITCODE
            if (Select-String -Path $out -Pattern '== result ==' -Quiet -ErrorAction SilentlyContinue) {
                $ok = $true
                Log "24h $($arm.Label) DONE (exit $code)"
            } else {
                Log "24h $($arm.Label) attempt $attempt unfinished (exit $code); retry in 60s"
                Start-Sleep -Seconds 60
            }
        }

        if ($ok) {
            Add-Content -Path $hour24State -Value $arm.Label
        } else {
            Log "WARNING: 24h $($arm.Label) abandoned after 3 attempts"
            Add-Content -Path $hour24State -Value "$($arm.Label)-ABANDONED"
        }
    }

    Set-Content -Path $hour24Flag -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Remove-Item $lockFile -ErrorAction SilentlyContinue
    Log '24h VALIDATION COMPLETE'

    # Fold the day's trades into the scoreboard so the headline numbers include them.
    $programStart = (Get-Content $startFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $programStart) { $programStart = '1970-01-01 00:00:00' }
    Log 'rewriting scoreboard to include the 24h run'
    & "$root\.venv\Scripts\python.exe" tools\final_report.py "--since=$programStart" > (Join-Path $root 'reports\logs\final_report.out') 2>&1
    exit 0
}

# Ordered: slippage decides go/no-go, so it runs first. Then what to buy, then how to
# sell, then the combined confirmation.
#
# These five are the AUTOMATIC stages. The sixth step -- the 24-hour validation -- is
# deliberately not in this list, because a list entry is something the program runs by
# itself. It is gated behind -RunHour24 and the approval file, above.
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
        Clear-StrayBots
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

# ---------------------------------------------------------------------------
# Stop here, on purpose. The five stages are done and the scoreboard is written, so this
# is the point where a decision has to be made by a person rather than by the schedule.
#
# The 24-hour validation is NOT started below (or anywhere else): reviewing the results
# may change what is worth spending a day on, and a long run started automatically would
# lock that choice in before anyone had read the numbers. To proceed, read
# reports\final_report.txt, write reports\logs\hour24_approved.txt naming the
# configuration to validate, then re-run this script with -RunHour24.
# ---------------------------------------------------------------------------
Log "AWAITING MANUAL APPROVAL: 24h test NOT started. Read reports\final_report.txt, write reports\logs\hour24_approved.txt, then re-run with -RunHour24"
Write-Output ''
Write-Output 'PROGRAM COMPLETE - five stages done, scoreboard written.'
Write-Output 'The 24-hour validation did NOT start. It is a manual gate:'
Write-Output "  1. read $root\reports\final_report.txt"
Write-Output '  2. write reports\logs\hour24_approved.txt naming the best config(s):'
Write-Output '         secs=86400'
Write-Output '         arm=best-entry2  MIN_CONFIRM_BUY_SOL=2'
Write-Output '  3. re-run: powershell -File tools\sweep_program.ps1 -RunHour24'
Write-Output ''
