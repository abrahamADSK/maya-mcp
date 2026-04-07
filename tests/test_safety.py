"""
test_safety.py
==============
Pytest suite for src/maya_mcp/safety.py — verifies that each of the 15 dangerous
patterns is detected, and that normal safe inputs pass through without warnings.

No Maya connection or external dependencies required.
Run with:
    pytest tests/test_safety.py -v
"""

import pytest
from maya_mcp.safety import check_dangerous


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_blocked(input_str: str) -> str:
    """Assert that check_dangerous returns a warning (not None)."""
    result = check_dangerous(input_str)
    assert result is not None, (
        f"Expected a safety warning but got None for input:\n  {input_str!r}"
    )
    return result


def assert_safe(input_str: str) -> None:
    """Assert that check_dangerous returns None (no warning)."""
    result = check_dangerous(input_str)
    assert result is None, (
        f"Expected no safety warning but got:\n  {result}\n"
        f"for input:\n  {input_str!r}"
    )


# ---------------------------------------------------------------------------
# Pattern 1 — cmds.file(new=True, force=True)
# ---------------------------------------------------------------------------

class TestNewSceneForce:
    def test_new_scene_force_true(self):
        """Pattern 1: new scene with force=True discards unsaved changes."""
        assert_blocked('cmds.file(new=True, force=True)')

    def test_new_scene_force_true_multiline(self):
        """Pattern 1: multiline variant still triggers."""
        assert_blocked('cmds.file(\n    new=True,\n    force=True\n)')

    def test_new_scene_without_force_safe(self):
        """new=True without force=True should pass safely."""
        assert_safe('cmds.file(new=True)')


# ---------------------------------------------------------------------------
# Pattern 2 — cmds.delete(cmds.ls())
# ---------------------------------------------------------------------------

class TestDeleteAllNodes:
    def test_delete_ls_all(self):
        """Pattern 2: deleting all nodes from cmds.ls() corrupts the scene."""
        assert_blocked('cmds.delete(cmds.ls())')

    def test_delete_ls_all_spaced(self):
        """Pattern 2: whitespace variation still triggers."""
        assert_blocked('cmds.delete( cmds.ls( ) )')


# ---------------------------------------------------------------------------
# Pattern 3 — Wildcard delete ('*')
# ---------------------------------------------------------------------------

class TestWildcardDelete:
    def test_delete_star(self):
        """Pattern 3: cmds.delete('*') removes all matching nodes."""
        assert_blocked("cmds.delete('*')")

    def test_delete_double_star(self):
        """Pattern 3: cmds.delete('**') also triggers."""
        assert_blocked('cmds.delete("**")')

    def test_delete_specific_name_safe(self):
        """Deleting a specific named node should pass safely."""
        assert_safe('cmds.delete("pSphere1")')


# ---------------------------------------------------------------------------
# Pattern 4 — undoInfo(stateWithoutFlush=False)
# ---------------------------------------------------------------------------

class TestUndoDisableWithoutFlush:
    def test_undo_state_without_flush_false(self):
        """Pattern 4: disabling undo without flush is unrecoverable."""
        assert_blocked('cmds.undoInfo(stateWithoutFlush=False)')

    def test_undo_state_without_flush_zero(self):
        """Pattern 4: numeric zero variant also triggers."""
        assert_blocked('cmds.undoInfo(stateWithoutFlush=0)')


# ---------------------------------------------------------------------------
# Pattern 5 — undoInfo(state=False)
# ---------------------------------------------------------------------------

class TestUndoDisableState:
    def test_undo_state_false(self):
        """Pattern 5: disabling undo state makes operations permanent."""
        assert_blocked('cmds.undoInfo(state=False)')

    def test_undo_state_zero(self):
        """Pattern 5: numeric zero variant also triggers."""
        assert_blocked('cmds.undoInfo(state=0)')

    def test_undo_open_chunk_safe(self):
        """openChunk is the safe alternative — should not trigger."""
        assert_safe('cmds.undoInfo(openChunk=True)')


# ---------------------------------------------------------------------------
# Pattern 6 — os.remove / os.unlink / shutil.rmtree
# ---------------------------------------------------------------------------

class TestFilesystemDeletion:
    def test_os_remove(self):
        """Pattern 6a: os.remove() deletes files with no undo."""
        assert_blocked('os.remove("/path/to/scene.ma")')

    def test_os_unlink(self):
        """Pattern 6b: os.unlink() is equivalent to os.remove()."""
        assert_blocked('os.unlink("/tmp/backup.mb")')

    def test_shutil_rmtree(self):
        """Pattern 6c: shutil.rmtree() recursively deletes directories."""
        assert_blocked('shutil.rmtree("/project/assets")')

    def test_os_path_exists_safe(self):
        """os.path.exists() is read-only — should not trigger."""
        assert_safe('os.path.exists("/path/to/file.ma")')


# ---------------------------------------------------------------------------
# Pattern 7 — Path traversal (../ or ..\\)
# ---------------------------------------------------------------------------

class TestPathTraversal:
    def test_path_traversal_unix(self):
        """Pattern 7a: Unix path traversal ../ triggers warning."""
        assert_blocked('file_path = "../../etc/passwd"')

    def test_path_traversal_windows(self):
        """Pattern 7b: Windows path traversal ..\\ triggers warning."""
        assert_blocked('file_path = "..\\\\secret\\\\file.ma"')

    def test_absolute_path_safe(self):
        """Absolute path without traversal should pass safely."""
        assert_safe('file_path = "/projects/hero/scenes/layout_v001.ma"')


# ---------------------------------------------------------------------------
# Pattern 8 — cmds.unloadPlugin
# ---------------------------------------------------------------------------

class TestUnloadPlugin:
    def test_unload_plugin(self):
        """Pattern 8: unloading plugin corrupts dependent nodes."""
        assert_blocked('cmds.unloadPlugin("AbcImport")')

    def test_load_plugin_safe(self):
        """Loading a plugin is safe — should not trigger."""
        assert_safe('cmds.loadPlugin("AbcImport")')


# ---------------------------------------------------------------------------
# Pattern 9 — removeNamespace with deleteNamespaceContent=True
# ---------------------------------------------------------------------------

class TestRemoveNamespace:
    def test_remove_namespace_delete_content(self):
        """Pattern 9: removing namespace + deleting content is destructive."""
        assert_blocked(
            'cmds.namespace(removeNamespace="character", '
            'deleteNamespaceContent=True)'
        )

    def test_remove_namespace_without_delete_safe(self):
        """Removing an empty namespace without deleteContent should pass."""
        assert_safe('cmds.namespace(removeNamespace="old_ns")')


# ---------------------------------------------------------------------------
# Pattern 10 — polyReduce on referenced geometry
# ---------------------------------------------------------------------------

class TestPolyReduceReferenced:
    def test_poly_reduce_referenced(self):
        """Pattern 10: reducing referenced geometry breaks reference chain."""
        assert_blocked('cmds.polyReduce("char:body_geo", referenced=True)')

    def test_poly_reduce_referenced_namespace(self):
        """Pattern 10: polyReduce on namespace geometry also triggers."""
        assert_blocked('cmds.polyReduce("hero_ns:mesh", namespace="hero_ns")')

    def test_poly_reduce_local_safe(self):
        """polyReduce on local (non-referenced) geometry should pass."""
        assert_safe('cmds.polyReduce("pSphere1", percentage=50)')


# ---------------------------------------------------------------------------
# Pattern 11 — mel.eval('source ...')
# ---------------------------------------------------------------------------

class TestMelSourceInjection:
    def test_mel_source_injection(self):
        """Pattern 11: mel.eval('source ...') executes arbitrary MEL."""
        assert_blocked("mel.eval('source \"/tmp/malicious.mel\"')")

    def test_mel_source_double_quotes(self):
        """Pattern 11: double-quote variant also triggers."""
        assert_blocked('mel.eval("source \\"/network/scripts/setup.mel\\"")')

    def test_mel_eval_command_safe(self):
        """mel.eval with a normal command should pass safely."""
        assert_safe('mel.eval("polySphere -r 1 -sx 20 -sy 20")')


# ---------------------------------------------------------------------------
# Pattern 12 — lockNode(lock=False) on system nodes
# ---------------------------------------------------------------------------

class TestUnlockNode:
    def test_unlock_node(self):
        """Pattern 12: unlocking nodes makes system nodes vulnerable."""
        assert_blocked('cmds.lockNode("persp", lock=False)')

    def test_lock_node_safe(self):
        """Locking a node (lock=True) should pass safely."""
        assert_safe('cmds.lockNode("myRig_ctrl", lock=True)')


# ---------------------------------------------------------------------------
# Pattern 13 — for x in cmds.ls(): cmds.delete
# ---------------------------------------------------------------------------

class TestBulkDeleteLoop:
    def test_loop_delete_all(self):
        """Pattern 13: iterating cmds.ls() and deleting destroys the scene."""
        assert_blocked(
            'for node in cmds.ls():\n    cmds.delete(node)'
        )

    def test_loop_delete_all_variant(self):
        """Pattern 13: single-char variable name variant."""
        assert_blocked(
            'for n in cmds.ls():\n    cmds.delete(n)'
        )

    def test_loop_delete_filtered_safe(self):
        """Deleting filtered nodes (by type) should pass safely."""
        assert_safe(
            'for node in cmds.ls(type="transform"):\n    cmds.delete(node)'
        )


# ---------------------------------------------------------------------------
# Pattern 14 — cmds.file(removeReference=True)
# ---------------------------------------------------------------------------

class TestRemoveReference:
    def test_remove_reference(self):
        """Pattern 14: removing a reference loses instanced geometry."""
        assert_blocked('cmds.file(rfn="characterRN", removeReference=True)')

    def test_remove_reference_spaced(self):
        """Pattern 14: whitespace variant still triggers."""
        assert_blocked(
            'cmds.file("char.ma", removeReference = True)'
        )

    def test_unload_reference_safe(self):
        """Unloading (not removing) a reference should pass safely."""
        assert_safe('cmds.file(rfn="characterRN", unloadReference=True)')


# ---------------------------------------------------------------------------
# Pattern 15 — Changing renderer away from Arnold
# ---------------------------------------------------------------------------

class TestRendererChange:
    def test_change_renderer_to_mayaSoftware(self):
        """Pattern 15: switching to mayaSoftware invalidates materials."""
        assert_blocked(
            "cmds.setAttr('defaultRenderGlobals.currentRenderer', "
            "'mayaSoftware', type='string')"
        )

    def test_change_renderer_to_redshift(self):
        """Pattern 15: switching to redshift also triggers warning."""
        assert_blocked(
            'cmds.setAttr("defaultRenderGlobals.currentRenderer", '
            '"redshift", type="string")'
        )

    def test_set_renderer_arnold_safe(self):
        """Setting renderer to arnold should pass safely."""
        assert_safe(
            'cmds.setAttr("defaultRenderGlobals.currentRenderer", '
            '"arnold", type="string")'
        )


# ---------------------------------------------------------------------------
# Safe input — normal operations must NOT be blocked
# ---------------------------------------------------------------------------

class TestSafeInputPasses:
    def test_create_polysphere(self):
        """Creating a polySphere is a safe, common operation."""
        assert_safe('cmds.polySphere(name="mySphere", radius=1.0)')

    def test_set_translation(self):
        """Setting translation on a named object is safe."""
        assert_safe('cmds.setAttr("pCube1.translateX", 5.0)')

    def test_select_objects(self):
        """Selecting objects is non-destructive."""
        assert_safe('cmds.select("pSphere1", "pCube1", replace=True)')

    def test_query_scene(self):
        """Querying the scene is read-only."""
        assert_safe('cmds.ls(type="mesh")')

    def test_save_scene(self):
        """Saving the scene is safe."""
        assert_safe('cmds.file(rename="/project/scenes/layout_v002.ma")')

    def test_undo_chunk(self):
        """Undo chunks are the safe alternative to disabling undo."""
        assert_safe(
            'cmds.undoInfo(openChunk=True)\n'
            'cmds.polySphere()\n'
            'cmds.undoInfo(closeChunk=True)'
        )

    def test_load_plugin(self):
        """Loading plugins is safe."""
        assert_safe('cmds.loadPlugin("AbcImport")')

    def test_create_namespace(self):
        """Creating a namespace is non-destructive."""
        assert_safe('cmds.namespace(add="character_ns")')

    def test_import_file(self):
        """Importing a file is a normal operation."""
        assert_safe(
            'cmds.file("/assets/hero.ma", i=True, namespace="hero")'
        )

    def test_set_keyframe(self):
        """Setting keyframes is non-destructive."""
        assert_safe(
            'cmds.setKeyframe("pSphere1", attribute="translateX", value=10.0)'
        )

    def test_arnold_renderer(self):
        """Setting Arnold renderer is the expected production renderer."""
        assert_safe(
            'cmds.setAttr("defaultRenderGlobals.currentRenderer", '
            '"arnold", type="string")'
        )


# ---------------------------------------------------------------------------
# Comprehensive — all 15 patterns trigger exactly as designed
# ---------------------------------------------------------------------------

class TestAll15Patterns:
    """
    Each tuple contains: (pattern_number, description, triggering_input).
    The test iterates all 15 and verifies each one produces a warning.
    """

    PATTERN_INPUTS = [
        (1,  "new scene force=True",
         'cmds.file(new=True, force=True)'),
        (2,  "delete all from cmds.ls()",
         'cmds.delete(cmds.ls())'),
        (3,  "wildcard delete '*'",
         "cmds.delete('*')"),
        (4,  "undoInfo stateWithoutFlush=False",
         'cmds.undoInfo(stateWithoutFlush=False)'),
        (5,  "undoInfo state=False",
         'cmds.undoInfo(state=False)'),
        (6,  "filesystem deletion os.remove",
         'os.remove("/path/to/file.ma")'),
        (7,  "path traversal ../",
         '../../etc/passwd'),
        (8,  "unloadPlugin",
         'cmds.unloadPlugin("AbcImport")'),
        (9,  "removeNamespace deleteContent",
         'cmds.namespace(removeNamespace="ns", deleteNamespaceContent=True)'),
        (10, "polyReduce referenced",
         'cmds.polyReduce("geo", referenced=True)'),
        (11, "mel.eval source injection",
         "mel.eval('source \"/tmp/setup.mel\"')"),
        (12, "lockNode lock=False",
         'cmds.lockNode("persp", lock=False)'),
        (13, "loop delete cmds.ls()",
         'for n in cmds.ls():\n    cmds.delete(n)'),
        (14, "removeReference=True",
         'cmds.file(rfn="charRN", removeReference=True)'),
        (15, "change renderer non-arnold",
         'cmds.setAttr("defaultRenderGlobals.currentRenderer", '
         '"mayaSoftware", type="string")'),
    ]

    @pytest.mark.parametrize(
        "pattern_num,description,trigger",
        PATTERN_INPUTS,
        ids=[f"pattern_{p:02d}" for p, _, _ in PATTERN_INPUTS],
    )
    def test_all_15_patterns(self, pattern_num, description, trigger):
        """Each of the 15 patterns triggers a warning on its designed input."""
        result = check_dangerous(trigger)
        assert result is not None, (
            f"Pattern {pattern_num} ({description}) did NOT trigger "
            f"for input: {trigger!r}"
        )
        assert "Safety check" in result, (
            f"Pattern {pattern_num} warning message has unexpected format:\n{result}"
        )
