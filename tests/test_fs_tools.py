"""Filesystem tools, and the guard that is the whole security story.

These run against the real repo paths rather than a tmp_path fixture, because
the thing under test is exactly whether config.BRAIN_DIR and the resolution of
'..' behave as expected on this machine. A fixture that reproduces the layout
would be testing the fixture.

Nothing here writes outside brain/ - the write tests use a file under brain/
and clean up after themselves.
"""

from __future__ import annotations

import pytest

from app import config
from app.llm import fs_tools


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict) -> bool:
    return bool(result.get("is_error"))


# --- the write guard --------------------------------------------------------


@pytest.mark.parametrize(
    "path,allowed",
    [
        # These mirror the cases the SDK-era guard was tested against, so the
        # rewrite is held to the same bar.
        ("brain/profile.md", True),
        ("briefs/2026-08-10.md", True),
        ("brain/nested/deep.md", True),
        ("brain/imported/claude/x.md", True),
        ("app/main.py", False),
        (".env", False),
        ("brain/../app/main.py", False),
        ("../escape.md", False),
        # Absolute paths must not bypass the check.
        ("C:/Windows/System32/drivers/etc/hosts", False),
        ("/etc/passwd", False),
        # Deep traversal that lands back inside a writable tree is fine; one
        # that lands outside is not.
        ("brain/../brain/goals.md", True),
        ("brain/../../jarvis-brain/app.py", False),
    ],
)
def test_write_confinement(path, allowed):
    assert fs_tools.is_writable(path) is allowed


@pytest.mark.parametrize("name", sorted(fs_tools.DENIED_NAMES))
def test_secrets_are_never_writable_even_inside_brain(name):
    """A secret filename must not be creatable in a writable tree either."""
    assert fs_tools.is_writable(f"brain/{name}") is False


# --- the read guard ---------------------------------------------------------


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("brain/goals.md", True),
        ("app/main.py", True),
        ("README.md", True),
        # .claude/settings.json denied these two under the SDK. .env was NOT
        # denied there, which was a real gap: it holds ANTHROPIC_API_KEY and the
        # Trading212 secrets, and an agent that quotes it into a brief writes a
        # credential into git.
        ("token.json", False),
        ("credentials.json", False),
        (".env", False),
        # The SDK pinned cwd to the repo but an absolute path could still walk
        # out. Reads are confined to the repo now.
        ("/etc/passwd", False),
        ("C:/Windows/System32/drivers/etc/hosts", False),
        # brain/ is a junction into the private jarvis-brain repo, so this is
        # the same file as brain/goals.md by another name. Allowing one spelling
        # and denying the other would be arbitrary, not safer.
        ("../jarvis-brain/brain/goals.md", True),
        # The rest of that sibling repo is still out of bounds: the junction
        # grants brain/ and briefs/, not the repo it happens to live in.
        ("../jarvis-brain/README.md", False),
        ("../jarvis-brain/.git/config", False),
    ],
)
def test_read_confinement(path, allowed):
    assert fs_tools.is_readable(path) is allowed


def test_denied_read_returns_an_error_not_the_contents():
    result = fs_tools.read_file({"path": ".env"})
    assert _is_error(result)
    assert "not permitted" in _text(result)


def test_search_never_matches_inside_a_denied_file():
    """A grep must not become a way to read a secret line by line.

    Asserted on the file half of each 'path:line: text' hit, not on the whole
    body: source that merely mentions '.env' is a legitimate match.
    """
    result = fs_tools.search({"pattern": "ANTHROPIC", "glob": "*"})
    files = {
        line.split(":", 1)[0] for line in _text(result).splitlines() if ":" in line
    }
    assert not (files & fs_tools.DENIED_NAMES)


def test_list_files_never_lists_a_denied_name():
    result = fs_tools.list_files({"pattern": "*"})
    listed = {line.rsplit("/", 1)[-1] for line in _text(result).splitlines()}
    assert not (listed & fs_tools.DENIED_NAMES)


def test_list_files_skips_the_virtualenv():
    """A listing buried under site-packages is useless to the model and slow."""
    body = _text(fs_tools.list_files({"pattern": "*"}))
    assert ".venv/" not in body
    assert ".git/" not in body


# --- writes go through the guard, structurally ------------------------------


def test_every_write_tool_refuses_an_outside_path():
    """The guard lives inside each writer, so no dispatch path skips it."""
    for name in sorted(fs_tools.WRITE_NAMES):
        result = fs_tools.dispatch(
            name, {"path": "app/main.py", "content": "x", "old": "a", "new": "b"}
        )
        assert _is_error(result), f"{name} allowed a write outside brain/"
        assert "not permitted" in _text(result), name


def test_write_and_append_round_trip(tmp_path):
    target = "brain/_fs_tools_test.md"
    path = config.BRAIN_DIR / "_fs_tools_test.md"
    try:
        assert not _is_error(fs_tools.write_file({"path": target, "content": "one\n"}))
        assert path.read_text(encoding="utf-8") == "one\n"

        assert not _is_error(fs_tools.append_file({"path": target, "content": "two"}))
        assert path.read_text(encoding="utf-8") == "one\ntwo\n"

        assert not _is_error(fs_tools.read_file({"path": target}))
    finally:
        path.unlink(missing_ok=True)


def test_edit_refuses_ambiguous_text():
    """Replacing text that appears twice would silently change the wrong one."""
    target = "brain/_fs_tools_edit.md"
    path = config.BRAIN_DIR / "_fs_tools_edit.md"
    try:
        fs_tools.write_file({"path": target, "content": "dup\ndup\n"})
        result = fs_tools.edit_file({"path": target, "old": "dup", "new": "x"})
        assert _is_error(result)
        assert "2 times" in _text(result)

        ok = fs_tools.edit_file({"path": target, "old": "dup\ndup", "new": "once"})
        assert not _is_error(ok)
        assert path.read_text(encoding="utf-8") == "once\n"
    finally:
        path.unlink(missing_ok=True)


def test_edit_reports_missing_text_rather_than_creating_it():
    target = "brain/_fs_tools_missing.md"
    path = config.BRAIN_DIR / "_fs_tools_missing.md"
    try:
        fs_tools.write_file({"path": target, "content": "hello\n"})
        result = fs_tools.edit_file({"path": target, "old": "nope", "new": "x"})
        assert _is_error(result)
        assert path.read_text(encoding="utf-8") == "hello\n"
    finally:
        path.unlink(missing_ok=True)


# --- specs ------------------------------------------------------------------


def test_specs_and_handlers_agree():
    assert set(fs_tools.SPECS) == set(fs_tools.HANDLERS)


def test_specs_can_be_narrowed_for_a_read_only_turn():
    names = {s["function"]["name"] for s in fs_tools.specs(fs_tools.READ_ONLY_NAMES)}
    assert names == set(fs_tools.READ_ONLY_NAMES)
    assert not (names & fs_tools.WRITE_NAMES)


def test_every_spec_is_a_closed_schema():
    """Constrained decoding needs additionalProperties off to be meaningful."""
    for spec in fs_tools.specs():
        assert spec["function"]["parameters"]["additionalProperties"] is False


def test_unknown_tool_is_an_error_not_a_crash():
    assert _is_error(fs_tools.dispatch("rm_rf", {}))
