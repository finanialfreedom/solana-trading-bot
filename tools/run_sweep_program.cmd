@echo off
REM Supervisor for the test program. Runs tools\sweep_program.ps1, and if that script
REM is ever killed (crash, antivirus, console signal) it starts it again, because the
REM script is resumable and skips stages it already completed. Stops only once the
REM program reports completion.
cd /d D:\AI\solana-trading-bot

:loop
if exist "reports\logs\program_complete.flag" goto done
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\AI\solana-trading-bot\tools\sweep_program.ps1" >> "D:\AI\solana-trading-bot\reports\logs\supervisor.log" 2>&1
if exist "reports\logs\program_complete.flag" goto done
echo [%DATE% %TIME%] sweep_program.ps1 exited; restarting in 60s >> "D:\AI\solana-trading-bot\reports\logs\supervisor.log"
timeout /t 60 /nobreak > nul
goto loop

:done
echo [%DATE% %TIME%] program complete >> "D:\AI\solana-trading-bot\reports\logs\supervisor.log"
