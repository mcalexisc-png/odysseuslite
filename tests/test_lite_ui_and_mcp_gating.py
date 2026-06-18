"""Regression guards for the lite-mode UI/MCP surface — Odysseus Lite.

Two former blockers are pinned here so they can't silently regress:

  1. The lite-served index.html must contain NO Email/Calendar/Gallery nav
     markup. Lite mode strips these server-side (app._strip_lite_html); a
     pattern that stops matching (e.g. an id rename in index.html) would let
     a removed feature reappear in the rail.

  2. The Email built-in MCP server (14 IMAP/SMTP tools) must NOT be registered
     when LITE_MODE=true. Email HTTP routes are gated off in lite, so exposing
     the same capability via the agent's MCP toolset would be an inconsistent
     back-door.

Both checks are pure (no app boot, no network): the strip transform runs on
the static HTML file, and the MCP gate is read from a reloaded module under a
patched env.
"""
import importlib
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "static" / "index.html"


from services.lite_html import strip_lite_html as _strip_lite_html


# ── 1. Served HTML has no email/calendar/gallery nav markup in lite ──────────

# (label, regex) — each must NOT survive the lite strip. Regexes target DOM
# elements (a tag with the id/attr), not JS string literals, so the harmless
# selector strings left in the inline gate script don't trip the test.
_FORBIDDEN_DOM = [
    ("email rail button",      r'<button[^>]+id="rail-email"'),
    ("calendar rail button",   r'<button[^>]+id="rail-calendar"'),
    ("gallery rail button",    r'<button[^>]+id="rail-gallery"'),
    ("calendar tool entry",    r'<div[^>]+id="tool-calendar-btn"'),
    ("gallery tool entry",     r'<div[^>]+id="tool-gallery-btn"'),
    ("email sidebar section",  r'<div[^>]+id="email-section"'),
    ("email settings tab",     r'<button[^>]+data-settings-tab="email"'),
]


def test_index_html_has_strip_targets_in_source():
    """Sanity: the markup the strip removes actually exists in the source.

    Guards against an id rename silently turning the strip into a no-op that
    still 'passes' the absence checks below.
    """
    src = INDEX_HTML.read_text(encoding="utf-8")
    for label, pattern in _FORBIDDEN_DOM:
        assert re.search(pattern, src), f"strip target missing from source: {label}"


def test_lite_served_html_omits_email_calendar_gallery_nav():
    src = INDEX_HTML.read_text(encoding="utf-8")
    stripped = _strip_lite_html(src)
    for label, pattern in _FORBIDDEN_DOM:
        assert not re.search(pattern, stripped), f"{label} still present in lite HTML"


def test_lite_strip_keeps_div_balance():
    """The strip must remove whole elements — not orphan a closing tag that
    would prematurely close .sidebar-inner and break sidebar layout."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    stripped = _strip_lite_html(src)
    opens = len(re.findall(r"<div\b[^>]*>", stripped))
    closes = stripped.count("</div>")
    assert opens == closes, f"unbalanced <div> after strip: {opens} open / {closes} close"


def test_full_mode_html_unchanged_targets_present():
    """Sentinel: without the strip (full mode), the nav markup is intact."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    for label, pattern in _FORBIDDEN_DOM:
        assert re.search(pattern, src), f"{label} unexpectedly absent in full-mode source"


# ── 2. Email MCP server excluded from _BUILTIN_SERVERS in lite ───────────────

def _builtin_servers_for(lite: bool):
    prev = os.environ.get("LITE_MODE")
    os.environ["LITE_MODE"] = "true" if lite else "false"
    try:
        import src.builtin_mcp as m
        importlib.reload(m)
        return set(m._BUILTIN_SERVERS.keys())
    finally:
        if prev is None:
            os.environ.pop("LITE_MODE", None)
        else:
            os.environ["LITE_MODE"] = prev
        # Restore module to ambient env so we don't leak lite state to others.
        import src.builtin_mcp as m
        importlib.reload(m)


def test_email_mcp_absent_in_lite_mode():
    servers = _builtin_servers_for(lite=True)
    assert "email" not in servers, f"email MCP server registered in lite: {servers}"
    # The other built-ins must still be present.
    assert {"image_gen", "memory", "rag"} <= servers


def test_email_mcp_present_in_full_mode():
    servers = _builtin_servers_for(lite=False)
    assert "email" in servers, f"email MCP server missing in full mode: {servers}"
