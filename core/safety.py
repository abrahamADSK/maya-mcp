"""
safety.py
=========
Dangerous pattern detection for Maya Python operations.

Scans tool parameters and code for patterns known to cause data loss,
scene corruption, or unintended side effects in Maya production scenes.

Based on fpt-mcp / flame-mcp's proven regex + explanation + alternative pattern.
Adapted for the Maya domain where the risk is corrupting production scenes,
losing undo history, or breaking file references.
"""

import re
from typing import Optional

# Each entry: (regex, explanation, safe_alternative)
_DANGEROUS_PATTERNS = [
    # -- Destructive scene operations -----------------------------------------
    (
        r'cmds\.file\s*\(\s*new\s*=\s*True.*force\s*=\s*True',
        "Creating a new scene with force=True discards all unsaved changes "
        "with no recovery possible.",
        "Check cmds.file(q=True, modified=True) first. If the scene has "
        "unsaved changes, prompt the user before discarding.",
    ),
    (
        r'cmds\.delete\s*\(\s*cmds\.ls\s*\(\s*\)\s*\)',
        "Deleting all nodes returned by cmds.ls() will remove system nodes "
        "and corrupt the scene beyond recovery.",
        "Use cmds.delete(cmds.ls(type='transform')) to only delete user "
        "objects, or specify exact node names.",
    ),
    (
        r'cmds\.delete\s*\(\s*["\'][\*]+["\']\s*\)',
        "Wildcard delete ('*') will remove all matching nodes including "
        "system nodes, potentially corrupting the scene.",
        "Use specific node names or filter by type first with cmds.ls().",
    ),
    # -- Undo system tampering ------------------------------------------------
    (
        r'cmds\.undoInfo\s*\(\s*stateWithoutFlush\s*=\s*(?:False|0)\s*\)',
        "Disabling the undo system means ALL operations from this point "
        "are unrecoverable. If the script crashes, work is lost.",
        "Keep undo enabled. Use cmds.undoInfo(openChunk=True) to group "
        "operations into a single undoable chunk instead.",
    ),
    (
        r'cmds\.undoInfo\s*\(\s*state\s*=\s*(?:False|0)\s*\)',
        "Disabling undo state — all subsequent operations become permanent.",
        "Use undo chunks (openChunk/closeChunk) instead of disabling undo.",
    ),
    # -- File system dangers --------------------------------------------------
    (
        r'os\.remove\s*\(|os\.unlink\s*\(|shutil\.rmtree\s*\(',
        "Direct filesystem deletion has no undo and no confirmation. "
        "This could permanently delete scene files or assets.",
        "Use Maya's file management (cmds.file) or prompt the user "
        "for confirmation before deleting files.",
    ),
    (
        r'\.\./|\.\.\\',
        "Path traversal detected — could access files outside the "
        "intended project directory.",
        "Use absolute paths or paths relative to the project root. "
        "Never include '..' in file paths.",
    ),
    # -- Plugin deregistration ------------------------------------------------
    (
        r'cmds\.unloadPlugin\s*\(',
        "Unloading a plugin while its nodes exist in the scene will "
        "corrupt those nodes (they become unknown/broken).",
        "Only unload plugins in an empty scene. Check for dependent "
        "nodes first with cmds.ls(type=pluginNodeTypes).",
    ),
    # -- Namespace dangers ----------------------------------------------------
    (
        r'cmds\.namespace\s*\(.*removeNamespace.*deleteNamespaceContent\s*=\s*True',
        "Removing a namespace with deleteNamespaceContent=True permanently "
        "deletes all nodes in that namespace with no undo.",
        "Move contents to root namespace first with "
        "cmds.namespace(moveNamespace=['ns',':']) then remove the empty namespace.",
    ),
    # -- Risky mesh operations ------------------------------------------------
    (
        r'cmds\.polyReduce.*(?:referenced|namespace)',
        "Reducing polygons on referenced geometry can break the reference "
        "chain and cause issues when the reference is updated.",
        "Duplicate the referenced geometry first, then reduce the copy.",
    ),
    # -- MEL source injection -------------------------------------------------
    (
        r'mel\.eval\s*\(\s*["\']source\s',
        "Sourcing MEL scripts via mel.eval('source ...') can execute "
        "arbitrary code from untrusted file paths.",
        "Only source MEL scripts from trusted, known locations within "
        "the project directory.",
    ),
    # -- Critical node locking ------------------------------------------------
    (
        r'cmds\.lockNode\s*\(.*lock\s*=\s*False',
        "Unlocking system nodes (like default cameras or render globals) "
        "makes them vulnerable to accidental deletion.",
        "Only unlock user-created nodes. Never unlock 'persp', 'top', "
        "'front', 'side', or 'defaultRenderGlobals'.",
    ),
    # -- Batch operations without limits --------------------------------------
    (
        r'for\s+\w+\s+in\s+cmds\.ls\s*\(\s*\)\s*:.*cmds\.delete',
        "Iterating over all nodes and deleting is extremely dangerous — "
        "will destroy system nodes and corrupt the scene.",
        "Always filter cmds.ls() by type or name pattern before "
        "performing bulk operations.",
    ),
    # -- Reference manipulation -----------------------------------------------
    (
        r'cmds\.file\s*\(.*removeReference\s*=\s*True',
        "Removing a reference is destructive — all instanced/duplicated "
        "geometry from that reference will be lost.",
        "Consider unloading the reference instead: "
        "cmds.file(unloadReference=refNode) — this preserves the link.",
    ),
    # -- Renderer changes in production scenes --------------------------------
    (
        r'cmds\.setAttr\s*\(\s*["\']defaultRenderGlobals\.currentRenderer["\']\s*,\s*["\'](?!arnold)',
        "Changing the renderer in a production scene may invalidate all "
        "material assignments and render settings.",
        "Verify with the user before switching renderers. Save the scene "
        "first as a backup.",
    ),
]


def check_dangerous(params_str: str) -> Optional[str]:
    """
    Scan serialized tool parameters or code for dangerous patterns.

    Args:
        params_str: Python code or JSON-serialized string of tool parameters.

    Returns:
        Formatted warning string if patterns found, None if safe.
    """
    hits = []
    for pattern, reason, alternative in _DANGEROUS_PATTERNS:
        if re.search(pattern, params_str, re.IGNORECASE | re.DOTALL):
            hits.append(f"  - {reason}\n    Safe alternative: {alternative}")

    if not hits:
        return None

    return (
        "Safety check — potentially dangerous pattern(s) detected:\n\n"
        + "\n\n".join(hits)
        + "\n\nReview and revise the code before proceeding. "
        "Use search_maya_docs to find the correct approach if unsure."
    )
