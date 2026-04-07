# Anti-Patterns and Common Hallucinations

Known incorrect patterns that LLMs frequently generate for Maya Python scripting.
This document helps the RAG system correct common mistakes before they reach Maya.

## Wrong Command Names

- `cmds.usdExport()` — WRONG. Correct: `cmds.mayaUSDExport()`
- `cmds.usdImport()` — WRONG. Correct: `cmds.mayaUSDImport()`
- `cmds.polyBevel()` — WRONG (deprecated). Correct: `cmds.polyBevel3()`
- `cmds.polyCreateFace()` — WRONG. Correct: `cmds.polyCreateFacet()`
- `cmds.polyExtrudeEdge()` — WRONG. Correct: `cmds.polyExtrudeFacet()` for faces or `cmds.polyExtrudeEdge` (exists but rarely used)
- `cmds.renderSetup()` — WRONG as a command. Use `import maya.app.renderSetup.model.renderSetup as renderSetup` module
- `cmds.arnoldRenderSettings()` — WRONG. Use `cmds.setAttr('defaultArnoldRenderOptions.attr', value)`
- `cmds.createShader()` — WRONG. Correct: `cmds.shadingNode('type', asShader=True)`
- `cmds.assignMaterial()` — WRONG. Correct: `cmds.sets(obj, forceElement=shadingGroup)`
- `cmds.importFile()` — WRONG. Correct: `cmds.file('path', i=True)`
- `cmds.exportSelected()` — WRONG. Correct: `cmds.file('path', exportSelected=True)`
- `cmds.polyObject()` — WRONG. Use specific primitives: `cmds.polyCube()`, `cmds.polySphere()`, etc.

## Wrong Flag Names

- `cmds.polyCube(width=1)` — WRONG. Correct: `w=1`
- `cmds.polyCube(height=1)` — WRONG. Correct: `h=1`
- `cmds.polyCube(depth=1)` — WRONG. Correct: `d=1`
- `cmds.polySphere(radius=1)` — WRONG. Correct: `r=1`
- `cmds.xform(translation=...)` — WRONG. Correct: `t=...` (short flag)
- `cmds.xform(rotation=...)` — WRONG. Correct: `ro=...`
- `cmds.xform(worldSpace=True)` — WRONG. Correct: `ws=True`
- `cmds.setKeyframe(time=1)` — WRONG. Correct: `t=1`
- `cmds.setKeyframe(value=5)` — WRONG. Correct: `v=5`
- `cmds.setKeyframe(attribute='tx')` — WRONG. Correct: `at='translateX'`
- `cmds.playblast(filename='...')` — WRONG. Correct: `f='...'`
- `cmds.playblast(format='avi')` — WRONG. Correct: `fmt='image'` or `fmt='avi'`
- `cmds.file(import=True)` — WRONG (`import` is Python keyword). Correct: `i=True`
- `cmds.file(export=True)` — WRONG. Correct: `exportAll=True` or `exportSelected=True`

## Wrong setAttr Syntax

- `cmds.setAttr('obj.translate', [1,2,3])` — WRONG. Lists are not valid.
  Correct: `cmds.setAttr('obj.translate', 1, 2, 3, type='double3')`
- `cmds.setAttr('obj.name', 'newName')` — WRONG. `name` is not a settable attr.
  Correct: `cmds.rename('obj', 'newName')`
- `cmds.setAttr('obj.color', (1,0,0))` — WRONG. Tuples are not valid.
  Correct: `cmds.setAttr('obj.color', 1, 0, 0, type='double3')`
- `cmds.setAttr('obj.customStr', 'text')` — WRONG without type.
  Correct: `cmds.setAttr('obj.customStr', 'text', type='string')`

## Wrong Return Value Assumptions

- `obj = cmds.polyCube()` → `obj` is a LIST `['pCube1', 'polyCube1']`, NOT a string.
  Correct: `obj = cmds.polyCube()[0]` for the transform name.
- `cmds.ls(selection=True)` → Returns empty list `[]` if nothing selected, NOT `None`.
- `cmds.getAttr('obj.translate')` → Returns `[(x, y, z)]` (list of tuple), NOT `[x, y, z]`.
  Correct: `x, y, z = cmds.getAttr('obj.translate')[0]`
- `cmds.xform('obj', q=True, t=True)` → Returns `[x, y, z]` (flat list), different from getAttr.

## Deprecated or Removed Commands

- `cmds.polyBevel()` — Deprecated in Maya 2018. Use `cmds.polyBevel3()`.
- `cmds.render()` with `-batch` flag — Use `cmds.arnoldRender(batch=True)` for Arnold.
- `cmds.softSelect(softSelectEnabled=True)` — Syntax changed. Use `cmds.softSelect(sse=True)`.
- `maya.utils.executeDeferred()` — Still works but for MCP use `cmds.evalDeferred()`.
- `cmds.polySplitRing()` — Still works but `cmds.polyCut()` is preferred in modern workflows.

## Dangerous Patterns

- `cmds.file(new=True, force=True)` without checking `cmds.file(q=True, modified=True)` first
  — Silently discards unsaved work with no recovery.
- `cmds.delete(cmds.ls())` — Deletes ALL nodes including system nodes, potentially corrupting the scene.
  Safe alternative: `cmds.delete(cmds.ls(type='transform'))` to only delete user objects.
- `cmds.undoInfo(stateWithoutFlush=False)` — Disables undo. If the script crashes, all
  operations since disabling are unrecoverable.
- `cmds.namespace(removeNamespace='ns', deleteNamespaceContent=True)` — Deletes everything
  in the namespace with no undo.
- `os.remove()` or `shutil.rmtree()` on Maya scene files — External deletion has no undo.
- `cmds.unloadPlugin('mtoa')` while Arnold materials exist — Corrupts material assignments.
- `mel.eval('source "/untrusted/path/script.mel"')` — Arbitrary code execution risk.

## Common Misconceptions

1. **"cmds functions are async"** — WRONG. All cmds functions are synchronous and block.
   In MCP context, wrap in `asyncio.to_thread()` to avoid blocking the event loop.

2. **"Maya Python uses Python 3 print()"** — Depends on Maya version.
   Maya 2022+ uses Python 3. Maya 2020-2021 uses Python 2 (print as statement).

3. **"cmds.select() returns the selection"** — WRONG. `cmds.select()` returns None.
   Use `cmds.ls(selection=True)` to get the current selection.

4. **"You can use pip install in Maya"** — Partially true. Maya has its own Python
   environment. `mayapy -m pip install package` works but may conflict with Maya's
   bundled packages.

5. **"Arnold's aiStandardSurface has a 'roughness' attribute"** — WRONG.
   The correct attribute is `specularRoughness`. There is also `diffuseRoughness`,
   `coatRoughness`, and `transmissionExtraRoughness` but NOT just `roughness`.

6. **"You can query node type with cmds.nodeType()"** — Correct, but it returns the
   SHAPE type, not the transform type. For a polyCube, it returns `'mesh'`, not `'polyCube'`.

7. **"cmds.ls(type='light') finds all lights"** — WRONG. Use `cmds.ls(lights=True)`.
   `type='light'` only finds legacy directional lights.
