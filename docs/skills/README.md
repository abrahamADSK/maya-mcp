# Claude Code skills owned by maya-mcp

Procedural knowledge for *driving Maya through this server* — the order of
operations and what fails silently. Distinct from the three other layers:

| Layer | Holds | Where |
|---|---|---|
| **Tool** | Side effects, enforcement, I/O | `src/maya_mcp/server.py` |
| **Skill** | The recipe: which tools, in what order, the traps | here |
| **RAG** | Reference manual: commands, flags, return types | `src/maya_mcp/docs/*.md` |
| **Memory** | Cross-session state and behavioural feedback | `~/.claude/.../memory/` |

A skill costs its `description` in **every** session and loads its body only when
the description matches the user's intent. Keep descriptions tight and triggery.

## Why they live here and not in `.claude/skills/`

`.gitignore` excludes `.claude/` wholesale in this repo, so a skill placed there
would be silently untracked. Keeping them under `docs/` makes them version
controlled and subject to the atomic-docs rule: **a skill ships in the same commit
as the code it describes.**

They are *activated* by symlinking into the user-level skills directory, which is
what makes them fire from any working directory — not only when the cwd is this
repo. Skill discovery follows symlinked directories (verified).

## Activating them on a fresh clone

```bash
for s in maya-headless-batch maya-scene-assembly maya-anim-transfer; do
  ln -s "$PWD/docs/skills/$s" ~/.claude/skills/"$s"
done
```

Remove with `rm ~/.claude/skills/<name>` — that deletes the symlink, never the
tracked content.

## Current skills

| Skill | Fires on |
|---|---|
| `maya-headless-batch` | Heavy or crash-prone work that should leave the Command Port: batch renders, cache export/import, multi-file sweeps |
| `maya-scene-assembly` | References — create/replace/version-swap, namespaces, deferred refs, and the save-after-scripted-open trap |
| `maya-anim-transfer` | Retarget and character animation: HumanIK, mocap/BVH, cycles and loops, reach semantics |

`catcher-passes` is a related skill that spans maya-mcp **and** flame-mcp; it is
not tracked here because it bundles a 63 MB virtualenv.

## Editing

Edit the file in this repo — the symlink means the change is live immediately, with
no reinstall and no MCP restart. That is the main reason procedure belongs in a
skill rather than in a tool docstring or the server `instructions` block, both of
which need a release and a full Claude Code restart to take effect.
