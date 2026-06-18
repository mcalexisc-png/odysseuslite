"""reset-password recovery path — Odysseus Lite overlay (Phase 7).

A missed temp admin password must not require wiping the database. These tests
cover AuthManager.admin_reset_password (the operator recovery primitive) and the
setup.py reset_password CLI wrapper.
"""
import os
import tempfile

import pytest


@pytest.fixture
def auth(monkeypatch):
    """An AuthManager backed by a throwaway auth.json."""
    d = tempfile.mkdtemp()
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", d)
    import importlib
    import src.constants as consts
    importlib.reload(consts)
    import core.auth as auth_mod
    importlib.reload(auth_mod)
    a = auth_mod.AuthManager(auth_path=os.path.join(d, "auth.json"))
    a.create_user("admin", "originalpass", is_admin=True)
    return auth_mod, a


def test_admin_reset_changes_hash(auth):
    auth_mod, a = auth
    h_before = a.users["admin"]["password_hash"]
    assert a.admin_reset_password("admin", "newsecret") is True
    h_after = a.users["admin"]["password_hash"]
    assert h_after != h_before
    assert auth_mod._verify_password("newsecret", h_after)
    assert not auth_mod._verify_password("originalpass", h_after)


def test_admin_reset_no_old_password_needed(auth):
    """Unlike change_password, this never checks the current password."""
    auth_mod, a = auth
    assert a.admin_reset_password("admin", "whatever") is True


def test_admin_reset_unknown_user(auth):
    _auth_mod, a = auth
    assert a.admin_reset_password("nobody", "x") is False


def test_admin_reset_rejects_empty_password(auth):
    _auth_mod, a = auth
    assert a.admin_reset_password("admin", "") is False


def test_cli_reset_with_explicit_password(auth, monkeypatch, capsys):
    auth_mod, a = auth
    import importlib
    import setup as setup_mod
    importlib.reload(setup_mod)
    # Point the CLI's AuthManager at the same auth file.
    monkeypatch.setattr(setup_mod, "AuthManager", auth_mod.AuthManager, raising=False)

    rc = setup_mod.reset_password(["admin", "--password", "cli-new-pass"])
    assert rc == 0
    # Re-read from disk to confirm persistence.
    fresh = auth_mod.AuthManager(auth_path=a.auth_path) if hasattr(a, "auth_path") else auth_mod.AuthManager()
    h = fresh.users["admin"]["password_hash"]
    assert auth_mod._verify_password("cli-new-pass", h)


def test_cli_reset_generates_random_when_none_given(auth, monkeypatch, capsys):
    auth_mod, a = auth
    import importlib
    import setup as setup_mod
    importlib.reload(setup_mod)
    monkeypatch.delenv("ODYSSEUS_RESET_PASSWORD", raising=False)
    # Non-interactive: no --password, not a TTY -> random generated + printed.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = setup_mod.reset_password(["admin"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Temporary password:" in out
