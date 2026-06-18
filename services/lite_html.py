"""Server-side HTML stripping for Odysseus Lite.

In lite mode the Email/Calendar/Gallery features have no backend routes, so
their nav markup is removed from index.html before it's served (rather than
shipped-then-hidden with JS, which races and leaves a flash of removed UI).

Kept as a standalone, import-light module so it can be unit-tested without
booting the whole app (importing app.py runs heavy init under conftest).
"""
import re

_LITE_STRIP_PATTERNS: "list | None" = None
_LITE_STRIP_BLOCKS: "list | None" = None


def _get_lite_strip_patterns():
    """Compile once: regex patterns for lite-hidden markup that has no nested
    same-tag children (rail buttons, the flat tool-list divs) — a non-greedy
    .*?</tag> is safe here because there's only one closing tag to find."""
    global _LITE_STRIP_PATTERNS
    if _LITE_STRIP_PATTERNS is not None:
        return _LITE_STRIP_PATTERNS
    _LITE_STRIP_PATTERNS = [
        # Single-line icon-rail buttons — matched by unique id attribute on the line.
        re.compile(r'[ \t]*<button[^>]+id="rail-email"[^>]*>.*?</button>\n?', re.DOTALL),
        re.compile(r'[ \t]*<button[^>]+id="rail-calendar"[^>]*>.*?</button>\n?', re.DOTALL),
        re.compile(r'[ \t]*<button[^>]+id="rail-gallery"[^>]*>.*?</button>\n?', re.DOTALL),
        # "Tools" sidebar list entries — <div class="list-item" id="tool-*-btn"> … </div>.
        # These are the actual visible launchers; the icon-rail buttons above are a
        # separate top bar. Both must be stripped for the feature to truly disappear.
        re.compile(r'[ \t]*<div[^>]+id="tool-calendar-btn"[^>]*>.*?</div>\n?', re.DOTALL),
        re.compile(r'[ \t]*<div[^>]+id="tool-gallery-btn"[^>]*>.*?</div>\n?', re.DOTALL),
        # Settings nav tab — <button ... data-settings-tab="email"> … </button>
        re.compile(r'[ \t]*<button[^>]+data-settings-tab="email"[^>]*>.*?</button>\n?', re.DOTALL),
        # Appearance → "Customize sidebar" visibility toggles for the removed
        # features. Each is a <label class="vis-row"> … </label> identified by
        # its data-ui-key. Left in place they're dead rows that toggle nothing.
        re.compile(r'[ \t]*<label class="vis-row">(?:(?!</label>).)*?data-ui-key="email-section".*?</label>\n?', re.DOTALL),
        re.compile(r'[ \t]*<label class="vis-row">(?:(?!</label>).)*?data-ui-key="tool-calendar".*?</label>\n?', re.DOTALL),
        re.compile(r'[ \t]*<label class="vis-row">(?:(?!</label>).)*?data-ui-key="tool-gallery".*?</label>\n?', re.DOTALL),
    ]
    return _LITE_STRIP_PATTERNS


def _get_lite_strip_blocks():
    """Compile once: (open-tag regex, html tag name) pairs for lite-hidden
    markup that DOES contain nested elements of the same tag (email-section
    has a nested <div>; the settings email panel has many). A non-greedy
    regex .*?</div> would stop at the first inner close and leave the
    element's real closing tag orphaned, which silently closes some later
    ancestor (e.g. .sidebar-inner) instead — corrupting layout. These are
    removed by walking tag depth instead of regex.
    """
    global _LITE_STRIP_BLOCKS
    if _LITE_STRIP_BLOCKS is not None:
        return _LITE_STRIP_BLOCKS
    _LITE_STRIP_BLOCKS = [
        (re.compile(r'[ \t]*<div[^>]+id="email-section"[^>]*>'), "div"),
        (re.compile(r'[ \t]*<!-- ═══ EMAIL TAB ═══ -->\n[ \t]*<div[^>]+data-settings-panel="email"[^>]*>'), "div"),
    ]
    return _LITE_STRIP_BLOCKS


def _remove_balanced_block(html: str, open_re, tag: str) -> str:
    """Remove one open_re match through its matching closing </tag>, counting
    nested open/close tags of the same name so inner elements don't fool a
    naive non-greedy regex into stopping early."""
    m = open_re.search(html)
    if not m:
        return html
    tag_re = re.compile(r'<(/?)' + tag + r'\b[^>]*>')
    depth = 0
    for tm in tag_re.finditer(html, m.start()):
        depth += 1 if tm.group(1) == "" else -1
        if depth == 0:
            end = tm.end()
            if html[end:end + 1] == "\n":
                end += 1
            return html[:m.start()] + html[end:]
    return html  # unbalanced source — leave untouched rather than risk corruption


def strip_lite_html(html: str) -> str:
    """Remove email/calendar/gallery markup from index.html when in lite mode."""
    for open_re, tag in _get_lite_strip_blocks():
        html = _remove_balanced_block(html, open_re, tag)
    for pattern in _get_lite_strip_patterns():
        html = pattern.sub("", html)
    return html
