"""Host file access detection — Odysseus Lite opt-in feature.

The agent can be given access to a folder on the user's computer by mounting it
into the container at /host (see docker-compose.lite.yml, HOST_FS_PATH/HOST_FS_MODE
in .env). This module is the single source of truth for *whether* such a mount is
active and in which mode, shared by the status endpoint (routes/workspace_routes.py)
and the startup warning (services/shell/sandbox.py).

The feature is OFF by default: when HOST_FS_PATH is unset, compose maps the already
-shared ./data dir at /host, which grants no new access. We detect that no-op case by
comparing /host's realpath to the data dir's, and report mode "none".
"""

from __future__ import annotations

import os
from typing import Dict

# In-container mount point for the opt-in host folder.
HOST_MOUNT = "/host"


def _data_dir() -> str:
    """Realpath of the project data dir (the inert default /host maps to)."""
    try:
        from src.constants import DATA_DIR
        return os.path.realpath(DATA_DIR)
    except Exception:
        return os.path.realpath(os.path.join(os.getcwd(), "data"))


def is_host_mounted() -> bool:
    """True when /host is a real directory distinct from the data dir.

    When HOST_FS_PATH is unset, compose mounts ./data at /host (a no-op), so an
    existing /host that resolves to the data dir is treated as "not mounted".
    """
    if not os.path.isdir(HOST_MOUNT):
        return False
    try:
        # samefile (dev+inode), not a realpath string compare: the inert default
        # bind-mounts the data dir at /host, so /host and the data dir are the SAME
        # directory but have DIFFERENT realpaths ("/host" vs "/app/data") because
        # each is its own mount point. Only dev+inode identity catches that no-op.
        return not os.path.samefile(HOST_MOUNT, _data_dir())
    except OSError:
        # _data_dir() missing or unreadable: a real, distinct /host still counts.
        return True


def is_host_writable() -> bool:
    """True when /host is mounted and the process can write to it (rw mount)."""
    return is_host_mounted() and os.access(HOST_MOUNT, os.W_OK)


def host_access_status() -> Dict[str, object]:
    """Report host-access state for the UI and startup logging.

    `mode` is the user-facing summary:
      - "none"       : no host folder shared (default)
      - "read-only"  : /host mounted but not writable, or shell still sandboxed
      - "read-write" : /host mounted writable AND the shell sandbox is off
                       (AGENT_SHELL_MODE=unrestricted + AGENT_SHELL_ACK_UNSAFE=true)

    Read-write requires BOTH a writable mount and an unsandboxed shell: a writable
    mount alone still leaves the file tools jailed and the shell sandboxed, so the
    blast radius isn't the full "agent can run anything against your files" tier.
    """
    mounted = is_host_mounted()
    writable = is_host_writable()

    try:
        from services.shell.sandbox import load_config
        cfg = load_config()
        shell_mode = cfg.mode
        unrestricted = cfg.effective_unrestricted
    except Exception:
        shell_mode = "sandboxed"
        unrestricted = False

    if not mounted:
        mode = "none"
    elif writable and unrestricted:
        mode = "read-write"
    else:
        mode = "read-only"

    return {
        "mounted": mounted,
        "writable": writable,
        "shell_mode": shell_mode,
        "shell_unrestricted": unrestricted,
        "mount_path": HOST_MOUNT,
        "mode": mode,
    }
