import asyncio
import sys
import time
import collections
from typing import Optional, Callable, Awaitable, Tuple, Dict
from src.constants import MAX_OUTPUT_CHARS

# --- Odysseus Lite overlay: agent shell sandbox -------------------------------
# The agent bash/python tools are the prompt-injection-reachable execution
# surface. The lite sandbox confines them (workspace jail, rlimits, no network,
# denylist) and applies a sane wall-clock timeout. See services/shell/sandbox.py.
from services.shell import sandbox as _sandbox

# Legacy upstream default was 1 hour; lite caps via AGENT_SHELL_TIMEOUT (120s).
DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour (upstream legacy; overridden by sandbox cfg)
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

def _effective_timeout(legacy_default: int) -> int:
    """Lite caps the timeout via AGENT_SHELL_TIMEOUT; unrestricted keeps legacy."""
    cfg = _sandbox.load_config()
    if cfg.effective_unrestricted:
        return legacy_default
    return cfg.timeout


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _truncate
        cfg = _sandbox.load_config()

        # Mode gates: off => unavailable; restricted => no shell.
        if not cfg.tool_available:
            return {"error": "bash: shell tool disabled (AGENT_SHELL_MODE=off)", "exit_code": 126}
        if not cfg.shell_enabled:
            return {"error": "bash: shell disabled in AGENT_SHELL_MODE=restricted "
                             "(file tools remain available, confined to the workspace)",
                    "exit_code": 126}

        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")

        spawn = _sandbox.wrap_bash(content, _subproc_env or {}, cfg)
        if spawn.denied:
            return {"error": f"bash: blocked destructive command (matched '{spawn.denied}')",
                    "exit_code": 126}

        timeout = _effective_timeout(DEFAULT_BASH_TIMEOUT)
        if spawn.use_shell:
            proc = await asyncio.create_subprocess_shell(
                spawn.argv[0],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn.env,
                cwd=spawn.cwd,
                preexec_fn=spawn.preexec_fn,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *spawn.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn.env,
                cwd=spawn.cwd,
                preexec_fn=spawn.preexec_fn,
            )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=timeout,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"bash: timed out after {timeout}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _truncate
        cfg = _sandbox.load_config()

        if not cfg.tool_available:
            return {"error": "python: tool disabled (AGENT_SHELL_MODE=off)", "exit_code": 126}

        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")

        spawn = _sandbox.wrap_python(content, (sys.executable or "python"), _subproc_env or {}, cfg)
        timeout = _effective_timeout(DEFAULT_PYTHON_TIMEOUT)
        proc = await asyncio.create_subprocess_exec(
            *spawn.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn.env,
            cwd=spawn.cwd,
            preexec_fn=spawn.preexec_fn,
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=timeout,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {timeout}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
