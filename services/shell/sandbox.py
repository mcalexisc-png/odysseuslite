# services/shell/sandbox.py
"""
Agent shell sandboxing — Odysseus Lite overlay.

This is a *lite-specific* security layer (not present upstream). The agent's
bash/python tools (src/agent_tools/subprocess_tools.py) and the shell service
(services/shell/service.py) run with the full permissions of the process user.
On a personal laptop run as the user's own account, a prompt injection from a
malicious document or web-search result can become arbitrary code execution
with the whole machine as blast radius.

This module wraps every agent-initiated process spawn in a constrained
environment, controlled by env:

    AGENT_SHELL_MODE = sandboxed (default) | restricted | off | unrestricted

  - sandboxed:   prefer a real OS sandbox (bubblewrap > firejail > nsjail) if
                 on PATH and functional; else fall back to a degraded-but-real
                 confinement: cwd+HOME jailed to data/agent_workspace/, a
                 destructive-command denylist, no network (unless AGENT_SHELL_NET),
                 plus resource.setrlimit (CPU + address space) and a wall-clock
                 timeout applied in the child.
  - restricted:  no shell at all; file tools confined to the workspace (the
                 dispatcher refuses bash; python is import-limited). Enforced by
                 the callers checking shell_enabled()/python_enabled().
  - off:         shell tool unavailable.
  - unrestricted: legacy behavior (no sandbox). Only honored when BOTH
                 AGENT_SHELL_MODE=unrestricted AND AGENT_SHELL_ACK_UNSAFE=true;
                 logs a loud warning at startup.

Other env:
    AGENT_SHELL_NET        = false (default)  — allow network egress from agent shell
    AGENT_SHELL_ACK_UNSAFE = false (default)  — required to actually run unrestricted
    AGENT_SHELL_TIMEOUT    = 120 (seconds)    — lite-sane wall-clock ceiling
    AGENT_SHELL_MAX_MEM_MB = 1024             — RLIMIT_AS ceiling for child (0 = off)
    AGENT_SHELL_CPU_SECONDS = 0               — RLIMIT_CPU ceiling (0 = use timeout only)

The design keeps the upstream code paths intact: callers ask this module to
*wrap* an argv (and supply a preexec_fn) rather than forking the upstream
subprocess logic. That keeps the upstream-sync surface small.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
MODE_SANDBOXED = "sandboxed"
MODE_RESTRICTED = "restricted"
MODE_OFF = "off"
MODE_UNRESTRICTED = "unrestricted"
_VALID_MODES = {MODE_SANDBOXED, MODE_RESTRICTED, MODE_OFF, MODE_UNRESTRICTED}

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_MEM_MB = 1024


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Destructive-command denylist (best-effort; defense in depth, not the only line)
# ---------------------------------------------------------------------------
# These are substring/word checks against the raw command. The real isolation
# is the OS sandbox / rlimits / workspace jail; this denylist just stops the
# most obvious self-inflicted footguns from a prompt injection before they
# even spawn. Intentionally conservative to avoid false positives on benign use.
_DENY_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf $home",
    ":(){:|:&};:",          # fork bomb (whitespace-stripped form checked too)
    "mkfs",
    "dd if=/dev/zero of=/dev",
    "dd if=/dev/random of=/dev",
    "> /dev/sda",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    "chmod -r 000 /",
    "chown -r",
)


def is_denied_command(command: str) -> Optional[str]:
    """Return the matched denylist pattern if the command is blocked, else None."""
    if not command:
        return None
    lowered = command.lower()
    compact = "".join(lowered.split())  # whitespace-insensitive for fork bombs etc.
    for pat in _DENY_PATTERNS:
        pat_compact = "".join(pat.split())
        if pat in lowered or pat_compact in compact:
            return pat
    return None


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SandboxConfig:
    mode: str
    net_enabled: bool
    ack_unsafe: bool
    timeout: int
    max_mem_mb: int
    cpu_seconds: int
    workspace: str
    launcher: Optional[str] = None          # resolved sandbox binary path, if any
    launcher_kind: Optional[str] = None     # "bwrap" | "firejail" | "nsjail" | None

    @property
    def effective_unrestricted(self) -> bool:
        """unrestricted only takes effect with the explicit ack."""
        return self.mode == MODE_UNRESTRICTED and self.ack_unsafe

    @property
    def shell_enabled(self) -> bool:
        # restricted = no shell; off = nothing
        return self.mode in {MODE_SANDBOXED, MODE_UNRESTRICTED}

    @property
    def tool_available(self) -> bool:
        return self.mode != MODE_OFF


def _default_workspace_dir() -> str:
    """Fallback agent workspace = data/agent_workspace/ under DATA_DIR."""
    try:
        from src.constants import DATA_DIR
        base = DATA_DIR
    except Exception:
        base = os.path.join(os.getcwd(), "data")
    ws = os.path.join(base, "agent_workspace")
    try:
        os.makedirs(ws, exist_ok=True)
    except Exception:
        pass
    return ws


def resolve_workspace() -> str:
    """The directory the agent shell is confined to *right now*.

    Prefer the per-turn active workspace (already vetted by
    src.tool_execution.vet_workspace), falling back to the persistent default
    data/agent_workspace/. Resolved per-call so concurrent turns each confine to
    their own folder; the launcher binds exactly this path writable.
    """
    try:
        from src.tool_execution import agent_cwd
        cwd = agent_cwd()
        if cwd and os.path.isdir(cwd):
            return os.path.realpath(cwd)
    except Exception:
        pass
    return _default_workspace_dir()


def _detect_launcher() -> tuple[Optional[str], Optional[str]]:
    """Find the best available OS sandbox launcher. Returns (path, kind)."""
    for kind in ("bwrap", "firejail", "nsjail"):
        path = shutil.which(kind)
        if path:
            return path, kind
    return None, None


_CONFIG: Optional[SandboxConfig] = None


def load_config(force: bool = False) -> SandboxConfig:
    """Read sandbox config from env once (cached). `force=True` re-reads (tests)."""
    global _CONFIG
    if _CONFIG is not None and not force:
        return _CONFIG

    raw_mode = (os.getenv("AGENT_SHELL_MODE") or MODE_SANDBOXED).strip().lower()
    if raw_mode not in _VALID_MODES:
        logger.warning(
            "AGENT_SHELL_MODE=%r is not valid (%s); falling back to 'sandboxed'.",
            raw_mode, ", ".join(sorted(_VALID_MODES)),
        )
        raw_mode = MODE_SANDBOXED

    launcher, kind = (None, None)
    if raw_mode == MODE_SANDBOXED:
        launcher, kind = _detect_launcher()

    _CONFIG = SandboxConfig(
        mode=raw_mode,
        net_enabled=_env_bool("AGENT_SHELL_NET", False),
        ack_unsafe=_env_bool("AGENT_SHELL_ACK_UNSAFE", False),
        timeout=max(1, _env_int("AGENT_SHELL_TIMEOUT", DEFAULT_TIMEOUT)),
        max_mem_mb=max(0, _env_int("AGENT_SHELL_MAX_MEM_MB", DEFAULT_MAX_MEM_MB)),
        cpu_seconds=max(0, _env_int("AGENT_SHELL_CPU_SECONDS", 0)),
        workspace=_default_workspace_dir(),
        launcher=launcher,
        launcher_kind=kind,
    )
    return _CONFIG


def reset_config_cache() -> None:
    """Drop the cached config (tests mutate env then call load_config(force=True))."""
    global _CONFIG
    _CONFIG = None


# ---------------------------------------------------------------------------
# Startup logging
# ---------------------------------------------------------------------------
def _host_mount_writable() -> bool:
    """True when the opt-in /host folder is mounted writable. Lazy import keeps
    sandbox.py free of a circular dependency (src.host_fs imports this module)."""
    try:
        from src.host_fs import is_host_writable
        return is_host_writable()
    except Exception:
        return False


def log_startup_mode() -> None:
    cfg = load_config(force=True)
    if cfg.effective_unrestricted:
        host_line = ""
        if _host_mount_writable():
            host_line = (
                "  A WRITABLE host folder is mounted at /host: the agent (and any\n"
                "  prompt injection it hits) can MODIFY or DELETE your real files.\n"
            )
        logger.warning(
            "============================================================\n"
            "  AGENT_SHELL_MODE=unrestricted (ACK'd) — the agent shell runs\n"
            "  with NO sandbox. A prompt injection can execute arbitrary code\n"
            "  as this user. Only use this on a trusted, isolated machine.\n"
            "%s"
            "============================================================",
            host_line,
        )
        return
    if cfg.mode == MODE_UNRESTRICTED and not cfg.ack_unsafe:
        logger.warning(
            "AGENT_SHELL_MODE=unrestricted requested but AGENT_SHELL_ACK_UNSAFE "
            "is not true — refusing to disable the sandbox; treating as 'sandboxed'."
        )
        return
    if cfg.mode == MODE_OFF:
        logger.info("AGENT_SHELL_MODE=off — agent shell/python tools are disabled.")
        return
    if cfg.mode == MODE_RESTRICTED:
        logger.info(
            "AGENT_SHELL_MODE=restricted — shell disabled; file tools confined to %s",
            cfg.workspace,
        )
        return
    # sandboxed
    if cfg.launcher_kind:
        logger.info(
            "AGENT_SHELL_MODE=sandboxed — using %s; network=%s; workspace=%s; timeout=%ss",
            cfg.launcher_kind, "on" if cfg.net_enabled else "off",
            cfg.workspace, cfg.timeout,
        )
    else:
        logger.info(
            "AGENT_SHELL_MODE=sandboxed — no OS sandbox (bwrap/firejail/nsjail) found; "
            "using degraded confinement (workspace jail + rlimits + denylist). "
            "network=%s; workspace=%s; timeout=%ss",
            "on" if cfg.net_enabled else "off", cfg.workspace, cfg.timeout,
        )


# ---------------------------------------------------------------------------
# preexec_fn — applied in the child before exec (Linux). Sets rlimits + new
# session so the whole process group can be killed on timeout.
# ---------------------------------------------------------------------------
def build_preexec(cfg: Optional[SandboxConfig] = None) -> Optional[Callable[[], None]]:
    """Return a preexec_fn applying rlimits, or None on platforms without them."""
    cfg = cfg or load_config()
    if cfg.effective_unrestricted:
        return None
    if sys.platform == "win32":
        return None
    try:
        import resource  # noqa: F401
    except Exception:
        return None

    max_mem_bytes = cfg.max_mem_mb * 1024 * 1024 if cfg.max_mem_mb else 0
    cpu_seconds = cfg.cpu_seconds

    def _preexec() -> None:  # pragma: no cover - runs in forked child
        import resource as _r
        try:
            os.setsid()
        except Exception:
            pass
        if cpu_seconds > 0:
            try:
                _r.setrlimit(_r.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
            except Exception:
                pass
        if max_mem_bytes > 0:
            try:
                _r.setrlimit(_r.RLIMIT_AS, (max_mem_bytes, max_mem_bytes))
            except Exception:
                pass
        # No new privileges via core dump / file size caps are intentionally
        # left default to avoid breaking legitimate workspace writes.

    return _preexec


# ---------------------------------------------------------------------------
# Command wrapping for OS sandbox launchers
# ---------------------------------------------------------------------------
@dataclass
class WrappedSpawn:
    """Everything a caller needs to spawn the (possibly wrapped) process."""
    argv: List[str]
    use_shell: bool                       # True -> create_subprocess_shell(argv[0])
    cwd: str
    env: dict = field(default_factory=dict)
    preexec_fn: Optional[Callable[[], None]] = None
    denied: Optional[str] = None          # set if the command hit the denylist


def _bwrap_args(cfg: SandboxConfig, ws: str) -> List[str]:
    """bubblewrap flags: read-only root, writable workspace + tmp, optional net off."""
    args = [
        cfg.launcher,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind", "/", "/",          # read-only view of the system
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", ws, ws,               # workspace is writable
        "--chdir", ws,
        "--setenv", "HOME", ws,
        "--setenv", "TMPDIR", "/tmp",
    ]
    if not cfg.net_enabled:
        args.append("--unshare-net")
    return args


def _firejail_args(cfg: SandboxConfig, ws: str) -> List[str]:
    args = [
        cfg.launcher,
        "--quiet",
        "--private=" + ws,
        "--private-tmp",
        f"--whitelist={ws}",
    ]
    if not cfg.net_enabled:
        args.append("--net=none")
    return args


def _nsjail_args(cfg: SandboxConfig, ws: str) -> List[str]:
    args = [
        cfg.launcher,
        "-Mo",
        "--chroot", "/",
        "--cwd", ws,
        "--rw",
        "--bindmount", f"{ws}:{ws}",
    ]
    if not cfg.net_enabled:
        args.append("--disable_clone_newnet")  # nsjail isolates net by default; explicit
    args.append("--")
    return args


def _launcher_prefix(cfg: SandboxConfig, ws: str) -> List[str]:
    if cfg.launcher_kind == "bwrap":
        return _bwrap_args(cfg, ws)
    if cfg.launcher_kind == "firejail":
        return _firejail_args(cfg, ws)
    if cfg.launcher_kind == "nsjail":
        return _nsjail_args(cfg, ws)
    return []


def wrap_bash(command: str, base_env: dict, cfg: Optional[SandboxConfig] = None) -> WrappedSpawn:
    """Wrap a shell command string for the agent BashTool."""
    cfg = cfg or load_config()
    cwd = resolve_workspace()          # honor the per-turn active workspace
    env = dict(base_env or {})
    env["HOME"] = cwd

    denied = is_denied_command(command)
    if denied and not cfg.effective_unrestricted:
        return WrappedSpawn(argv=[command], use_shell=True, cwd=cwd, env=env, denied=denied)

    if cfg.effective_unrestricted or not cfg.launcher_kind:
        # No OS launcher (or unrestricted): run directly. Confinement for the
        # non-unrestricted case still comes from cwd=workspace + preexec rlimits.
        return WrappedSpawn(
            argv=[command],
            use_shell=True,
            cwd=cwd,
            env=env,
            preexec_fn=build_preexec(cfg),
        )

    prefix = _launcher_prefix(cfg, cwd)
    # Run the user's command under /bin/sh -c inside the sandbox.
    argv = prefix + ["/bin/sh", "-c", command]
    return WrappedSpawn(
        argv=argv,
        use_shell=False,
        cwd=cwd,
        env=env,
        preexec_fn=build_preexec(cfg),
    )


def wrap_python(code: str, python_exe: str, base_env: dict,
                cfg: Optional[SandboxConfig] = None) -> WrappedSpawn:
    """Wrap a python -I -c invocation for the agent PythonTool."""
    cfg = cfg or load_config()
    cwd = resolve_workspace()
    env = dict(base_env or {})
    env["HOME"] = cwd

    inner = [python_exe or sys.executable or "python", "-I", "-c", code]

    if cfg.effective_unrestricted or not cfg.launcher_kind:
        return WrappedSpawn(
            argv=inner,
            use_shell=False,
            cwd=cwd,
            env=env,
            preexec_fn=build_preexec(cfg),
        )

    prefix = _launcher_prefix(cfg, cwd)
    return WrappedSpawn(
        argv=prefix + inner,
        use_shell=False,
        cwd=cwd,
        env=env,
        preexec_fn=build_preexec(cfg),
    )
