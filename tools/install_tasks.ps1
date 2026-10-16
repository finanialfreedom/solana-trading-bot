# Recreate or repair the two scheduled tasks with settings that cannot stall.
#
# Registers:
#   SolanaBotSweepProgram  every 20 min: runs the sweep plan, resuming where it stopped
#   SolanaBotConnWatch     every 15 min: probes the connection and self-heals if down
#
# The settings matter as much as the command. Plain `schtasks /Create` silently applies
# defaults that stop an unattended run: it refuses to start on battery, kills the task
# if the laptop is unplugged, and skips a missed run instead of catching up. Those three
# defaults alone can end a night of testing with no error anywhere. This script sets
# them correctly, and re-running it repairs a task that has drifted.
#
# Safe to re-run at any time. Run it before any unattended session.

$ErrorActionPreference = 'Stop'
$root = 'D:\AI\solana-trading-bot'

if (-not (Test-Path $root)) { throw "workspace not found: $root" }

# No DisallowStartIfOnBatteries, no StopIfGoingOnBatteries, StartWhenAvailable on.
# ExecutionTimeLimit zero means no upper bound, so a long plan is never cut off.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

function Install-Task {
    param(
        [string]$Name,
        [string]$Execute,
        [string]$Argument,
        [int]$EveryMinutes,
        [string]$Description
    )

    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Argument -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
    # RepetitionDuration omitted on purpose: the task repeats indefinitely.

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Force -Description $Description | Out-Null

    $task = Get-ScheduledTask -TaskName $Name
    Write-Output ("{0,-24} every {1,-6} state {2}" -f $task.TaskName,
        $task.Triggers.Repetition.Interval, $task.State)
}

Install-Task -Name 'SolanaBotSweepProgram' `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\AI\solana-trading-bot\tools\sweep_program.ps1"' `
    -EveryMinutes 20 `
    -Description 'Resumable pump.fun dry-run test plan; restarts itself if killed'

Install-Task -Name 'SolanaBotConnWatch' `
    -Execute "$root\tools\conn_watch.cmd" `
    -Argument '' `
    -EveryMinutes 15 `
    -Description 'Probes RPC + WebSocket and self-heals the connection if it drops'

Write-Output ''
Write-Output 'settings check (all must be False/True/True):'
foreach ($name in 'SolanaBotSweepProgram', 'SolanaBotConnWatch') {
    $s = (Get-ScheduledTask -TaskName $name).Settings
    Write-Output ("  {0,-24} battery-start={1,-5} battery-stop={2,-5} catch-up={3,-5}" -f `
        $name, $s.DisallowStartIfOnBatteries, $s.StopIfGoingOnBatteries, $s.StartWhenAvailable)
}
Write-Output ''
Write-Output 'note: both tasks run only while the user is logged on (Interactive).'
Write-Output 'Staying logged on is enough. Logging off stops them.'
