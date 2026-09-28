---
name: maya-scene-assembly
description: Build or update a Maya scene assembled from referenced published files — create/replace references, namespaces, deferred (unloaded) refs, version swaps via the Toolkit breakdown. Fire before any reference operation and before saving a scene you opened by script. E.g. "mete el personaje en el shot", "actualiza a la última versión", "cambia esta referencia", "monta la escena del shot".
---

# Scene assembly from references

Applies to **any** production Maya scene whose contents come from other published
files — shots, layout, set dressing, character loading. The failure modes here
were paid for in-vivo; none of them raise an error when they happen.

For *which* `cmds.file` flag to use, ask `search_maya_docs`. This skill is the
**order of operations and what corrupts a scene quietly**.

## 1. The deferred-reference save trap

**This is the one that costs real work.**

A reference can sit in a scene *unloaded* (deferred) — present in the structure,
absent from memory. Artists keep heavy scenes workable that way on purpose.

Opening that scene **by script can pull those references into memory**. If you
then save, you have baked them in, and handed back a scene that is heavier than
the one the artist built and no longer matches their intent.

**The rule:**

- After any scripted open, **check reference load state before you save**, and
  unload whatever was unloaded when you found it.
- Better: **do not save at all** unless saving is the point of the task. A read
  operation that ends in a save is a bug, not a convenience.

## 2. Reference or import?

`maya_import_file` **imports**: the geometry becomes part of your scene
permanently, with no link back to the source. Right for one-off geometry, mocap,
and generated assets (Vision3D / WorldLabs output).

A **reference** keeps the link, so upstream republishes can flow in. Right for
anything a pipeline publishes — characters in shots, sets, layout, props.

The test is one question: **will somebody republish this upstream?** If yes, it
must be a reference. Importing it instead severs the shot from the asset, and
nobody notices until the character changes and the shot doesn't.

Note that maya-mcp's own demo history is import-heavy. That was a toy-scene
artefact, not a pattern to copy.

## 3. Use the dedicated tool

**`maya_reference` is the route.** Five operations:

| operation | does | needs |
|---|---|---|
| `create` | references the file in, namespaced | `file_path`, optional `namespace` |
| `list` | every reference node + file + namespace + loaded state | — |
| `replace` | repoints a node at a new file (**the version swap**) | `reference_node`, `file_path` |
| `load` / `unload` | drops or restores content, keeping the link | `reference_node` |

`list` first: it returns the `reference_node` the other four require, and its
`loaded` flag is how you spot the deferred-reference trap in §5.

Removal is **deliberately absent** — it is destructive and `safety.py` demands
explicit user confirmation. If a reference genuinely must go, ask the user.

The tool already carries a 120 s budget, so the old timeout trap does not apply
to it. It is still a **main-thread** operation: the GUI is frozen while a heavy
rig loads, and anything that loops over many scenes belongs out of process
(`maya-headless-batch`).

Dropping to `maya_session(action="execute_python")` with raw `cmds.file` is now
a fallback, not the route — reach for it only for a flag the tool does not
expose, and raise `timeout` when you do.

## 4. Namespace every reference

Always pass a namespace. An unnamespaced reference collides with whatever is
already in the scene, and Maya resolves the collision by renaming — so the damage
surfaces much later as broken constraints and connections quietly pointing at the
wrong node.

The namespace is also the handle for everything downstream: selection, weight
transfer, constraint targets, and telling two copies of the same asset apart.

Namespace destruction is **code-enforced**, not your job to remember:
`safety.py` blocks `removeNamespace(..., deleteNamespaceContent=True)` and offers
the safe alternative — move contents to the root namespace first, then remove the
empty namespace.

## 5. Version updates go through the Toolkit breakdown

**Do not rewrite reference paths by hand.** The sanctioned path for "update this
to the latest version" is the Toolkit breakdown updater, which resolves against
the published-file registry.

Hand-editing a path bypasses that registry, and the scene then asserts a
provenance ShotGrid does not agree with — a divergence that is invisible in Maya
and expensive to untangle downstream. On the ShotGrid side this is enforced too:
fpt-mcp hard-blocks `PublishedFile` path rewrites, and deliberate migrations go
through a deliberate script with a revert registry, never an ad-hoc edit.

## 6. Read before you write

- `maya_session action=list_scene` and `action=scene_snapshot` answer structural
  questions with no Python at all. Reach for them first.
- For provenance — which file a node came from, whether it is loaded, what
  namespace it carries — `maya_reference` with `operation="list"` returns all of
  it structured, in one call.

## 7. Removal is destructive

`cmds.file(..., removeReference=True)` is blocked by the safety layer because
everything instanced or duplicated from that reference is lost with it. **Unload
instead** — it is reversible, and it is almost always what was actually wanted.

## What this skill is not

- **Not one-off geometry import.** OBJ/FBX/GLB/Alembic/BVH into the open scene is
  `maya_import_file`; Vision3D and WorldLabs results have their own dispatch
  actions.
- **Not batch processing across many scenes.** That is `maya-headless-batch`,
  which also covers why a loop must not run on the Command Port.
- **Not publishing.** Registering the result is `maya_session action=publish`
  (native tk-multi-publish2), which captures dependencies itself.
- **Not an API reference.** Flag and return-value lookup is `search_maya_docs`.
