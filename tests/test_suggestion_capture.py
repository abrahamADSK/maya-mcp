"""Unit tests for the read-only console's improvement-suggestion capture.

The maya-mcp console spawns a `claude` subprocess with every file-mutation
tool denied (``DISALLOWED_TOOLS``), so it cannot edit the repo. Instead it is
instructed to emit ``@@SUGGESTION@@ ...`` lines, which the trusted worker pulls
out of the reply and appends to a local backlog file (the only writer of it).
These tests cover that pure capture / strip / append logic — imported from the
Qt-free ``console._readonly`` module, so no PySide / Maya is needed.
"""

from console._readonly import DISALLOWED_TOOLS, capture_suggestions


def test_captures_and_strips_single_suggestion(tmp_path):
    dest = tmp_path / "CONSOLE_IMPROVEMENTS.md"
    text = (
        "Done. Imported the mesh.\n"
        "@@SUGGESTION@@ cache the api_graph :: introspection is re-read per call\n"
        "Anything else?"
    )
    clean, n = capture_suggestions(text, dest)
    assert n == 1
    assert "@@SUGGESTION@@" not in clean
    assert "Done. Imported the mesh." in clean
    assert "Anything else?" in clean
    body = dest.read_text(encoding="utf-8")
    assert "cache the api_graph :: introspection is re-read per call" in body
    assert body.startswith("# Console improvement backlog")


def test_no_marker_leaves_text_and_writes_nothing(tmp_path):
    dest = tmp_path / "CONSOLE_IMPROVEMENTS.md"
    clean, n = capture_suggestions("Just a normal reply.", dest)
    assert n == 0
    assert clean == "Just a normal reply."
    assert not dest.exists()


def test_multiple_calls_append_header_only_once(tmp_path):
    dest = tmp_path / "CONSOLE_IMPROVEMENTS.md"
    capture_suggestions("@@SUGGESTION@@ one :: first idea", dest)
    _, n = capture_suggestions("@@SUGGESTION@@ two :: second idea", dest)
    assert n == 1
    body = dest.read_text(encoding="utf-8")
    assert body.count("# Console improvement backlog") == 1
    assert "one :: first idea" in body
    assert "two :: second idea" in body


def test_disallowed_tools_block_every_mutation_vector():
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
        assert tool in DISALLOWED_TOOLS
