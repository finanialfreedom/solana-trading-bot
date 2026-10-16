"""Can the bot actually reach Solana right now -- and fix it if not.

Two connections matter:

  HTTP RPC   carries the buys and the sells
  WebSocket  carries the live trade stream the confirm window and trailing stop need

If either is blocked the bot keeps "running" but is blind: no trades, no errors, no
clue. That happened twice via Norton, so this script does not just report the problem.

  --log    one silent probe, one timestamped line, exit 0/1
  --watch  probe; if the connection is down, repair it and carry on unattended
           (flush the DNS cache, retry with escalating waits, and if a sweep arm has
           wedged against the dead network, restart the sweep so it picks up again)

Prints no secrets.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets
from solana.rpc.async_api import AsyncClient

from config import RPC_URL, RPC_URL_LOG, WSS_URL, WSS_URL_LOG

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "reports" / "logs"
CONN_LOG = LOG_DIR / "connectivity.log"
PROGRAM_LOG = LOG_DIR / "program.log"
COMPLETE_FLAG = LOG_DIR / "program_complete.flag"

SWEEP_TASK = "SolanaBotSweepProgram"

# How long a running arm may go without touching its log before the connection watcher
# treats it as wedged and restarts the sweep. A 30-minute block logs trades constantly,
# so five quiet minutes means it is stuck.
ARM_STALL_SECONDS = 300

# Processes that make up "the sweep". The supervisor is matched as well as the arm: a
# running supervisor owns the single-instance lock, so counting only the arm made
# keep_job_alive() think the job was dead during the gaps between arms (and while an arm
# slept between retries) and launch a second supervisor. Two supervisors then fought
# over the same ports and log files, each killing the other's arm mid-block, which is
# how protect2 spent hours restarting instead of finishing.
#
# Matched on the script path, and the probing shell excludes its own PID, because the
# probe's command line contains this pattern and would otherwise always match itself.
#
# The bot itself is launched as `python -c "import asyncio, bot ..."`, which matches
# none of the script paths. It must be included explicitly: when an arm is killed, that
# grandchild otherwise survives and keeps trading, writing to the same reports\*.csv as
# the arm the supervisor starts next, so both trade logs are corrupted.
SWEEP_PROC_PATTERN = (
    r"interleaved_sweep\.py|sweep_program\.ps1|run_sweep_program\.cmd|import asyncio, bot"
)
SELF_GUARD = "$_.ProcessId -ne $PID -and "


def emit(line: str) -> None:
    """Print for the task log and append to connectivity.log."""
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with CONN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def stamp(msg: str) -> str:
    return f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"


def run_cmd(args: list[str], timeout: int = 60) -> str:
    """Run a helper command, never raise, return whatever it said."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (done.stdout or "") + (done.stderr or "")
    except Exception as exc:  # noqa: BLE001 - a failed repair must not crash the watcher
        return f"<{type(exc).__name__}: {exc}>"


async def http_test() -> bool:
    print(f"HTTP RPC  {RPC_URL_LOG}")
    for attempt in range(1, 4):
        t0 = time.perf_counter()
        try:
            async with AsyncClient(RPC_URL) as client:
                resp = await asyncio.wait_for(client.get_latest_blockhash(), timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  OK   blockhash received in {ms:.0f} ms")
            return True
        except Exception as exc:  # noqa: BLE001 - report whatever the network says
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> HTTP RPC is NOT reachable")
    return False


async def ws_test() -> bool:
    print(f"WebSocket {WSS_URL_LOG}")
    for attempt in range(1, 4):
        t0 = time.perf_counter()
        try:
            async with websockets.connect(WSS_URL, open_timeout=15, close_timeout=5) as ws:
                await ws.send(
                    '{"jsonrpc":"2.0","id":1,"method":"blockSubscribe",'
                    '"params":["all"]}'
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  OK   connected and got a frame in {ms:.0f} ms ({len(raw)} bytes)")
            return True
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL attempt {attempt} after {ms:.0f} ms: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2)
    print("  -> WebSocket is NOT reachable (the live trade stream would be dark)")
    return False


async def probe() -> bool:
    """Silent version of both tests. Returns True only if both work."""
    import contextlib
    import io

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        http_ok = await http_test()
        ws_ok = await ws_test()
    return http_ok and ws_ok


# ---------------------------------------------------------------------------
# Repair tools. All best-effort: this watcher must never be the thing that fails.
# ---------------------------------------------------------------------------

def flush_dns() -> None:
    """A stale resolver entry is the cheapest thing to rule out, and needs no admin."""
    out = run_cmd(["ipconfig", "/flushdns"])
    ok = "successfully" in out.lower() or "flushed" in out.lower()
    emit(stamp(f"heal: dns cache flush {'ok' if ok else 'no-op'}"))


def sweep_alive() -> bool:
    """Is the sweep running (supervisor or an experiment arm)?

    Counting the supervisor too is essential: between arms, and during the 30s sleep
    between a stage's retry attempts, no arm exists but the job is very much alive.
    Seeing "nothing running" in those windows made keep_job_alive() start a second
    supervisor, and the pair would kill each other's arms.
    """
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { " + SELF_GUARD
        + "$_.CommandLine -match '" + SWEEP_PROC_PATTERN + "' } | Measure-Object | "
        "Select-Object -ExpandProperty Count"
    )
    out = run_cmd(["powershell", "-NoProfile", "-Command", ps]).strip()
    try:
        return int(out) > 0
    except ValueError:
        return False


def arm_log_stall_seconds() -> float | None:
    """Seconds since the newest experiment log was written, or None if there is none."""
    logs = sorted(LOG_DIR.glob("exp_*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    return time.time() - logs[-1].stat().st_mtime


def start_sweep(note: str) -> None:
    out = run_cmd(["schtasks", "/Run", "/TN", SWEEP_TASK])
    ok = "success" in out.lower()
    emit(stamp(f"heal: sweep restart ({note}) {'ok' if ok else 'failed'}"))


def restart_wedged_arm() -> None:
    """Kill a wedged sweep (supervisor and arm), so the job is restarted clean.

    Only called when the network was down and the newest arm log has been silent well
    past a normal logging gap, which is the signature of an arm blocked on the socket.
    The supervisor is killed alongside the arm so the single-instance lock is released
    and the retry starts from a clean process; keep_job_alive() then re-runs the task,
    and the program resumes at the first stage it has not completed.
    """
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { " + SELF_GUARD
        + "$_.CommandLine -match '" + SWEEP_PROC_PATTERN + "' } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    run_cmd(["powershell", "-NoProfile", "-Command", ps])
    # The supervisor is gone now, so drop its lock or the restart would exit immediately.
    try:
        (LOG_DIR / "program.lock").unlink(missing_ok=True)
    except OSError:
        pass
    emit(stamp("heal: wedged sweep killed; restarting clean"))


def keep_job_alive() -> None:
    """The sweep is the thing we are protecting, so make sure it is actually running."""
    if COMPLETE_FLAG.exists():
        return
    if sweep_alive():
        return
    start_sweep("not running")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

async def log_mode() -> int:
    """One silent probe, appended to connectivity.log."""
    ok = await probe()
    emit(stamp("OK   RPC + WebSocket reachable" if ok else "FAIL RPC or WebSocket blocked"))
    return 0 if ok else 1


async def watch_mode() -> int:
    """Probe, repair if needed, make sure the job is still moving. No user involvement."""
    if await probe():
        emit(stamp("OK   RPC + WebSocket reachable"))
        keep_job_alive()
        return 0

    emit(stamp("FAIL RPC or WebSocket blocked - attempting self-repair"))

    # Escalating repairs: clear the resolver cache, then wait progressively longer for
    # the block to lift. Nothing here needs the user, a password, or a dialog.
    for round_no, wait_s in enumerate((5, 20, 60, 120), start=1):
        flush_dns()
        await asyncio.sleep(wait_s)
        if await probe():
            emit(stamp(f"HEALED after round {round_no} (~{wait_s}s wait)"))
            stall = arm_log_stall_seconds()
            if stall is not None and stall > ARM_STALL_SECONDS:
                restart_wedged_arm()
            keep_job_alive()
            return 0

    emit(stamp("STILL DOWN after 4 repair rounds (~205s)"))

    # The network stayed dead long enough that any arm holding it is now useless.
    stall = arm_log_stall_seconds()
    if stall is not None and stall > ARM_STALL_SECONDS:
        restart_wedged_arm()
    keep_job_alive()
    return 1


async def main() -> None:
    if "--watch" in sys.argv:
        raise SystemExit(await watch_mode())
    if "--log" in sys.argv:
        raise SystemExit(await log_mode())
    http_ok = await http_test()
    print()
    ws_ok = await ws_test()
    print()
    if http_ok and ws_ok:
        print("Both connections are up. The bot can trade for real.")
    else:
        print("If either says FAIL, Norton is still blocking and the bot cannot trade for real.")


if __name__ == "__main__":
    asyncio.run(main())
