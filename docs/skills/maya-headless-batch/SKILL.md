---
name: maya-headless-batch
description: Run heavy Maya work out-of-process with mayapy/kick instead of the Command Port — batch renders, cache export/import (Alembic, USD, FBX, OBJ), multi-file edits, validation sweeps. Fire when work spans many files or frames, when an op already froze or crashed the GUI, or when Maya must stay untouched. E.g. "exporta el alembic de todos los shots", "renderiza la secuencia", "pasa esto por todas las escenas", "sin tocar mi Maya".
---

# Heavy Maya work, out of process

Applies to **any** long or risky Maya operation, not one project's render. The
measurements here were taken in-vivo on Maya 2027 / macOS Tahoe; none of it is
inferable from the API docs.

For *which* command or flag to call, use `search_maya_docs` — that is the
reference layer. This skill is the **decision of where to run, and what fails
silently when you run it in the wrong place**.

## 1. The Command Port is Maya's main thread

`maya_session(action="execute_python")` executes **synchronously on Maya's main
thread**. Default wait is 10 s, `timeout` raises it to 600 s, heartbeats stream
every 10 s. For the whole of that window the GUI is frozen.

So a long op is not "slow" — it is a hang that looks like a crash, and on Maya
2027 / Tahoe it frequently becomes one. Three GUI crash modes are known on that
build (OpenGL paint, SynHub syscall, Qt hide), and chained heavy operations have
taken the session down on their own.

**Leave the Command Port when any of these is true:**

- the work touches more than one scene file, or loops over frames;
- it opens or saves scenes in sequence;
- it renders anything;
- it exports or imports a cache;
- the artist's session must survive, or must not be disturbed.

Staying on the Command Port is right for: inspecting the open scene, small edits
the artist wants to *see*, anything that must act on unsaved in-memory state.
That last one is the real constraint — a headless run only sees what is on disk.

## 2. What `maya.standalone` does not give you

**Plugins do not auto-load.** This is the number-one headless failure. A bare
standalone session loads none of them, so `cmds.AbcExport`, `cmds.mayaUSDExport`,
`cmds.FBXExport` and `cmds.arnoldExportAss` simply **do not exist** until you load
their plugin. The symptom is an `AttributeError` on a command you know is real.

Canonical pipeline set — load each in its own `try`, never abort the run on one:

```
mayaUsdPlugin   AbcImport   AbcExport   fbxmaya   objExport   mtoa
```

`mtoa` is licence-gated and slow to load headlessly; a failure there is normal on
an unlicensed box and must stay non-fatal.

**glTF and OBJ register no commands.** They are file translators driven through
`cmds.file(type="OBJ")` / `cmds.file(type="glTF Import")`. Do not go hunting for
a missing `cmds.gltfExport` — it never existed.

**There is no UI.** Playblast, viewport capture and every Qt path are
unavailable. If you need a moving picture from a headless run, render frames and
encode them; do not reach for playblast.

## 3. Rendering: Maya exports, kick renders

**Never write a rendered file from inside interactive Maya.** Both
`renderWindowEditor(writeImage=…)` and `cmds.render()` produce **scene-linear**
output and ignore every colour setting you apply.

Measured on a flat `surfaceShader` patch of exactly 0.5:

| Writer | Result | Meaning |
|---|---|---|
| Render View dump / `cmds.render()` | **127** | 0.5 written raw — no transform |
| `kick` | **188** | 0.5 sRGB-encoded — transform applied |

Every lever was tried on the 127 path (output-transform name, on/off, scene view
transform, `renderWindowEditor`'s own `viewTransformName` / `outputColorManage`,
the Arnold driver enum, `outputTarget="renderer"`). None moved it. A geometry
change *did* alter the image, so the renders were live, not cached — the
transform genuinely never applies on that path.

The working shape: **Maya exports a `.ass`** with the colour policy and output
path baked in, and **`kick` renders it out of process**.

```
kick -i <scene.ass> -as <AA> -r <W> <H> -dw -dp
```

`imageFilePrefix` is baked into the `.ass` with animation OFF so Arnold writes
exactly the path you asked for. **Never pass `-o`** — it fights the baked prefix.

Side benefits that matter on Tahoe: no Render View window is ever opened (one
less Qt window on a build that crashes on `QWidget::setVisible(false)`), Maya's
main thread only does a fast scene export, and the render itself cannot hang the
UI.

**Use the tools before writing your own**: `maya_session action=render_still`
already does this for one frame, and `catcher-passes` does it for a full
beauty + per-light + AOV pass set. Hand-roll only for a shape neither covers.

## 4. Interchange: verify the round-trip before anything depends on it

The export is not the deliverable — the **re-import** is. Caches fail quietly:
they open, they contain the right object names, and the animation is missing.

- **Verify early, against ground truth.** Compare the element count **and at
  least one varying channel**. A count-only check passes happily on a cache where
  every frame is identical — which is exactly the most common failure.
- **Never chain export → import → clear in one session.** If the export was
  wrong, that sequence destroys the evidence you need to diagnose it.
- **Verify before optimizing.** Establish the round-trip is faithful first, then
  make it fast. Optimizing an unverified pipeline just makes the wrong answer
  arrive sooner.
- Remember §2: `AbcExport`, `FBXExport`, `mayaUSDExport` are plugin commands and
  are absent headless until loaded.

## 5. The runner

`scripts/mayapy_runner.py` in this skill directory is the scaffold: standalone
init, guarded plugin loading, per-file work with per-file error isolation, and
teardown. Copy it, replace `do_work`, run it with mayapy:

```
/Applications/Autodesk/maya2027/Maya.app/Contents/bin/mayapy \
    ~/.claude/skills/maya-headless-batch/scripts/mayapy_runner.py --help
```

Resolve the real mayapy from the Maya the pipeline is pinned to, not "the newest
installed" — the launcher authority is FPT's configured Software version.

## What this skill is not

- **Not the catcher / relight render recipe.** Beauty + per-light layers +
  shadow catchers + AOVs into one multichannel EXR is the `catcher-passes` skill.
  Do not reimplement it here.
- **Not scene assembly.** References, namespaces and version swaps are
  `maya-scene-assembly`.
- **Not an API reference.** Command, flag and return-value lookup is
  `search_maya_docs`.
- **Not a bypass of the safety layers.** `safety.py` and `_ast_validate.py` still
  govern anything routed through `execute_python`; running headless is a change of
  venue, not of rules.
