"""Host file access — default-off guarantee.

The opt-in "share a folder with the agent" feature (src/host_fs.py, /host mount,
HOST_FS_PATH/HOST_FS_MODE) must be OFF unless the user explicitly shares a folder.
A fresh build mounts no real host path: compose maps ./data at /host as an inert
default, and the status must report mode "none".

Also re-asserts the unchanged safety invariant that vet_workspace() still rejects
a filesystem root, so the feature can't be turned into whole-disk access.
"""
import os
import tempfile

import src.host_fs as host_fs
from src.tool_execution import vet_workspace


def test_status_none_when_host_absent(monkeypatch):
    """No /host directory at all => mode 'none', not mounted."""
    monkeypatch.setattr(host_fs, "HOST_MOUNT", "/nonexistent/__no_host__")
    status = host_fs.host_access_status()
    assert status["mounted"] is False
    assert status["writable"] is False
    assert status["mode"] == "none"


def test_status_none_when_host_equals_data_dir(monkeypatch, tmp_path):
    """The inert compose default maps ./data at /host. Detect that no-op:
    when /host resolves to the data dir, report 'none' (no new access)."""
    data = tmp_path / "data"
    data.mkdir()
    # /host points at the same dir the data volume already shares.
    monkeypatch.setattr(host_fs, "HOST_MOUNT", str(data))
    monkeypatch.setattr(host_fs, "_data_dir", lambda: os.path.realpath(str(data)))
    status = host_fs.host_access_status()
    assert status["mounted"] is False
    assert status["mode"] == "none"


def test_status_none_when_same_dir_but_realpaths_differ(monkeypatch, tmp_path):
    """The real container case: /host and the data dir are the SAME directory
    (same dev+inode) but have DIFFERENT realpath strings, because each is its own
    bind-mount point ("/host" vs "/app/data" — neither is a symlink, so neither
    realpath collapses). A realpath string compare wrongly reports a shared folder;
    only samefile() (dev+inode) catches the no-op. We can't create two bind mounts
    in a unit test, so we stub samefile to model that exact situation."""
    data = tmp_path / "data"
    data.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setattr(host_fs, "HOST_MOUNT", str(host))
    monkeypatch.setattr(host_fs, "_data_dir", lambda: str(data))
    # Distinct realpaths (a string compare would say "different => mounted")...
    assert os.path.realpath(str(host)) != os.path.realpath(str(data))
    # ...but they are the same underlying directory (what bind mounts produce).
    monkeypatch.setattr(host_fs.os.path, "samefile", lambda a, b: True)
    status = host_fs.host_access_status()
    assert status["mounted"] is False, status
    assert status["mode"] == "none", status


def test_status_read_only_when_real_folder_mounted_and_shell_sandboxed(monkeypatch, tmp_path):
    """A real shared folder + sandboxed shell => 'read-only' even if writable."""
    data = tmp_path / "data"
    data.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(host_fs, "HOST_MOUNT", str(shared))
    monkeypatch.setattr(host_fs, "_data_dir", lambda: os.path.realpath(str(data)))
    # Sandboxed shell (the default): not unrestricted.
    monkeypatch.setenv("AGENT_SHELL_MODE", "sandboxed")
    monkeypatch.delenv("AGENT_SHELL_ACK_UNSAFE", raising=False)
    import services.shell.sandbox as sandbox
    sandbox.reset_config_cache()
    status = host_fs.host_access_status()
    assert status["mounted"] is True
    assert status["mode"] == "read-only"


def test_status_read_write_requires_writable_and_unrestricted(monkeypatch, tmp_path):
    """'read-write' only when the mount is writable AND the shell is unrestricted."""
    data = tmp_path / "data"
    data.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(host_fs, "HOST_MOUNT", str(shared))
    monkeypatch.setattr(host_fs, "_data_dir", lambda: os.path.realpath(str(data)))
    monkeypatch.setenv("AGENT_SHELL_MODE", "unrestricted")
    monkeypatch.setenv("AGENT_SHELL_ACK_UNSAFE", "true")
    import services.shell.sandbox as sandbox
    sandbox.reset_config_cache()
    try:
        status = host_fs.host_access_status()
        assert status["mounted"] is True
        assert status["writable"] is True
        assert status["mode"] == "read-write"
    finally:
        sandbox.reset_config_cache()


def test_filesystem_root_still_rejected():
    """Safety invariant unchanged: the whole disk can never be a workspace."""
    assert vet_workspace("/") is None
