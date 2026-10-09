import asyncio
import os
import signal
import subprocess


async def _terminate_process_tree(process):
    if process.returncode is not None:
        return

    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.communicate()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if process.returncode is None:
        process.kill()
    await process.wait()


async def run_subprocess(args, timeout, env=None):
    """Run a text subprocess and reliably remove its process tree."""
    kwargs = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(*args, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _terminate_process_tree(process)
        raise subprocess.TimeoutExpired(args, timeout) from exc
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process_tree(process))
        raise

    return subprocess.CompletedProcess(
        args=args,
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )
