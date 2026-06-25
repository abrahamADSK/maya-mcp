# Changelog

All notable changes to **maya-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Earlier releases (v0.1.0 … v1.3.0) are tagged in git but were not captured
in this file. Only v1.4.0 onward is documented here; consult `git log v1.3.0`
and the `HANDOFF.md` "Sesión N" blocks for history prior to that.

## [Unreleased]

### Changed
- **Console system prompt: explicit INTENT→ACTION map + "version" disambiguation
  (`console/claude_worker.py`).** The matcher is the LLM reading
  `--append-system-prompt`; robustness across phrasings now comes from an explicit
  intent vocabulary instead of relying on the user knowing tool names. Natural
  language like *"publish / sube el asset"* → `maya_session action=publish` and
  *"turntable / giratoria / review turntable"* → `maya_session action=review_turntable`.
  The publish/turntable mapping that was triplicated (workflow step 6, RULES, and the
  maya server block) is **consolidated** into one denser block in the maya server
  block (net ≈ +150 prompt tokens; mostly in the session-cached prefix). The
  "NEVER hand-build the playblast" guard is preserved.
- **Console system prompt: disambiguate the two meanings of "version" (fpt block).**
  A ShotGrid **Version** entity is *review media* (`sg_uploaded_movie` streaming,
  `sg_path_to_movie`, `sg_path_to_frames`) and is now kept distinct from **file
  versioning** (`PublishedFile.version_number` → `_v###`, automatic inside publish).
  "Create a turntable review version" now routes to a `sg_create` type=Version +
  `sg_upload`→`sg_uploaded_movie` recipe (using `review_turntable`'s returned
  `version_code`) rather than depending on the tool's returned note. Guarded by
  `tests/test_system_prompt.py` (+2 tests).

## [1.18.4] — 2026-06-24

### Fixed
- **`review_turntable` no longer frames non-geometry → empty turntable.** With no
  explicit `objects`/selection the recipe framed *every* top-level assembly,
  including non-renderable nodes. In-vivo (DJ Model) the published scene held an
  `aiGaussianSplat`-free `aiSkyDomeLight` (`GI_skydome`, default bbox ~±1000); its
  huge bounds dominated `exactWorldBoundingBox`, so the camera framed a ~2000-unit
  object and the actual model (~2 units) rendered as a speck → a visibly **empty**
  `.mov` (the container was a valid 1920×1080/100f, only the *content* was empty).
  The default now frames the **renderable mesh geometry** (mesh-shape parents),
  falling back to assemblies only when the scene has no mesh. Verified in-vivo by
  inspecting an actual frame; guarded by `tests/test_review_build.py`.

### Changed
- **`maya_session action=publish` returns a leaner payload (context hygiene).** The
  `publish` result dropped the full per-`(item,task)` `requested` tree and the
  all-passed `validation` list (kept only an `activation` count summary + the
  `published` deliverables + any `failures`); `preview` dropped `tree_pformat` and
  the verbose per-plugin `description`/`item_filters`. The old payload could be
  large enough to bloat the console's context and slow/stall its next request
  (Chat 72). No information the LLM acts on is lost.

## [1.18.3] — 2026-06-24

### Fixed
- **Console system prompt now reflects the real Maya tool hierarchy.** The
  in-Maya console (`console/claude_worker.py::build_system_prompt`) hard-coded a
  stale tool inventory — pre-dispatcher flat names (`maya_execute_python`,
  `maya_launch`, `maya_list_scene`, `vision3d_health`…) — and never listed the
  `maya_session` dispatcher, `review_turntable`, `publish`, or `maya_worldlabs`.
  Asked for a review turntable, the console LLM therefore **improvised** a
  playblast via `execute_python` instead of calling the deterministic
  `maya_session action=review_turntable` → it produced an **empty/mis-framed**
  `.mov` and ballooned the request to ~482k tokens (observed in-vivo on the DJ
  Model task). The prompt now lists the dispatcher hierarchy (maya_session +
  actions, maya_vision3d, maya_worldlabs, search_maya_docs) plus a hard rule:
  review-turntable and publish are deterministic tools, **never** hand-built with
  `execute_python` (a hand-built playblast yields empty frames and can hang Maya's
  main thread). Guarded by `tests/test_system_prompt.py` (pins the hierarchy,
  forbids the stale flat names).

## [1.18.2] — 2026-06-24

### Fixed
- **`.env` is now actually loaded — secret handling is coherent with fpt-mcp.**
  maya-mcp declared `python-dotenv` as a dependency and its `.env.example` +
  tool hints promised a repo-root `.env`, but nothing ever called `load_dotenv`,
  so `WORLDLABS_API_KEY`, `GPU_API_KEY` and `GPU_API_URL` were only read if
  already exported in the process environment — a bare `.env` was silently
  ignored. Added `load_dotenv(override=False)` at package import
  (`src/maya_mcp/__init__.py`), mirroring fpt-mcp (which is the canonical pattern:
  secrets in a git-ignored `.env`, read via `os.environ`). `override=False` is
  deliberate — process-env values (e.g. the console-injected `SHOTGRID_PROJECT_ID`,
  Chat 69, or a shell-exported key) win over `.env` and are never clobbered.

### Added
- **`WORLDLABS_API_KEY` documented in `.env.example`.** The World Labs (Marble)
  connector's key was missing from the env template; added a value-less entry
  with a "never commit a real key" note (closes the doc gap from the v1.18.0
  `maya_worldlabs` feature).

## [1.18.1] — 2026-06-24

### Fixed
- **`review_turntable` no longer passes the non-existent `maintainRatio` flag to
  `cmds.playblast`.** Maya 2027's `playblast` has no such flag, so the turntable
  raised `TypeError: Invalid flag 'maintainRatio'` and produced no `.mov` — a
  regression shipped in v1.18.0 (the recipe had no test exercising the real
  `playblast` call). The 16:9 square-pixel guarantee already comes from the
  camera (Film Aspect 1.778 + square pixels + Overscan film fit) plus an explicit
  16:9 `widthHeight`; the flag was redundant *and* invalid. Validated in-vivo on
  Maya 2027 (1920×1080 / 25 fps `.mov`, scene restored, no main-thread hang) and
  guarded by a new `tests/test_review_build.py` that whitelists the real
  `cmds.playblast` flag set (closes the mock blindspot that let the bug ship).

## [1.18.0] — 2026-06-24

### Added
- **`maya_worldlabs` dispatcher (16th tool) — World Labs (Marble) Gaussian-splat
  environments.** Image→world generation via the Marble API, SPZ→PLY conversion
  (gsbox), and an in-Maya build recipe validated in-vivo (Maya 2027 + MtoA 5.6.2
  / Arnold 7.5.2): `aiGaussianSplat` (Arnold render) + a coloured native
  point-cloud proxy (render-excluded, VP2.0 navigation) + `aiGaussianSplatShader`
  (emission/diffuse) + an eye-level camera centred in the world (ground from a
  low Y-percentile, +1.5 m) + an optional fake-HDR panorama dome (light-linked to
  exclude the splat). Actions: `health` / `generate` (confirm-before-spend cost
  guardrail) / `poll` / `download` / `convert` / `build`. New modules
  `worldlabs/{tool,maya_build}.py` + a gsbox SPZ→PLY branch in `convert.py`; 33
  tests in `tests/test_worldlabs_tool.py`. The `build` action runs inside Maya;
  generation spends World Labs credits only with `confirm=true`.
- **`maya_session(action="review_turntable")` — deterministic review turntable.**
  Codifies the model-review playblast so it never depends on the LLM improvising
  (which hung the MCP console: a playblast that captured an Arnold/IPR panel
  saturated Maya's main thread and timed out). Frames the model, orbits 360° over
  a range at the chosen fps, sets 16:9 / square pixels / overscan, and playblasts
  in **Viewport 2.0 offScreen pinned to the captured panel** (never Arnold) → a
  .mov, with an avfoundation→PNG fallback; leaves the scene untouched (restores
  panel camera/renderer + time unit, deletes the turntable nodes). Returns a
  Version code `{Asset}_{Task}` so the review Version is named after the task it
  was generated in (new module `review_build.py`). Multi-agent reviewed — the
  `editorPanelName` hang-repro bug was caught and fixed before merge.

## [1.17.0] — 2026-06-24

### Added
- **Per-call token-usage monitoring.** Each `claude -p` turn now logs its token
  usage (input context + cache + reasoning output) to the shared
  `~/Library/Logs/mcp-console-usage.log` via `console._readonly.log_usage`, so
  request weight is objectively visible across consoles. Covered by
  `tests/test_suggestion_capture.py`.

### Changed
- **Console request is much lighter — deferred tool loading + per-console MCP
  scoping.** The spawned `claude` subprocess now (1) runs with
  `ENABLE_TOOL_SEARCH=true`, so MCP tool schemas are deferred (only tool names
  load upfront; the model fetches a schema on demand via `ToolSearch`), and (2)
  is launched with `--strict-mcp-config --mcp-config` carrying only the servers
  the Maya console needs — Maya + ShotGrid (`fpt-mcp`), NOT Flame. Flame's ~38
  tool schemas no longer bloat every request. Together this cuts the per-request
  payload from ~49k tokens toward ~8–12k, addressing the slow/overloaded "no
  response" hangs. `console._readonly.build_scoped_mcp_config` builds the curated
  config; covered by `tests/test_suggestion_capture.py`.
- **CI Python matrix realigned to the real runtimes.** The test matrix now runs
  `[3.13, 3.14]` (Maya 2027 ships 3.13; the dev venv is 3.14) instead of
  `[3.10, 3.11, 3.12]`, which tested zero versions Maya actually deploys on.
  `requires-python` raised to `>=3.13`; the ruff/mypy/verify_concepts jobs and
  the Codecov upload pin moved to 3.13; the README requirement was corrected.

## [1.16.0] — 2026-06-23

### Changed
- **Console panel runs read-only (recording-safe)** — the spawned `claude`
  subprocess is now launched with `--disallowedTools Edit Write MultiEdit
  NotebookEdit Bash`, so it can no longer modify the repository. MCP tools and
  Read stay available, so Maya/ShotGrid/Flame work and RAG self-learning is
  unaffected (`learn_pattern` is a server-side MCP tool, not an agent file
  edit). Code-improvement ideas are captured, not applied: the agent emits
  `@@SUGGESTION@@ <title> :: <detail>` lines that `console._readonly.
  capture_suggestions` appends to the git-ignored `CONSOLE_IMPROVEMENTS.md`
  backlog (for a later dev session / PR) and strips from the reply. Covered by
  `tests/test_suggestion_capture.py`.

### Fixed
- **Console mirrors the user's language per message** — the spawned `claude`
  subprocess inherited the global `CLAUDE.md` "Spanish by default" bias and
  replied in Spanish to English orders. The console system prompt now carries an
  explicit LANGUAGE directive that overrides any inherited default and re-detects
  the latest message's language every turn.

## [1.15.0] — 2026-06-22

### Added
- **Console binds fpt-mcp ShotGrid ops to the Toolkit engine's project**
  (Chat 69). When Maya is launched via `tank` into a Task/Asset/Shot, the
  embedded console now reads the `tk-maya` engine's project at launch
  (`console/project_context.py::resolve_engine_project`) and injects it as
  `SHOTGRID_PROJECT_ID` into the spawned `claude` subprocess, so fpt-mcp ops
  (e.g. `tk_publish`) target the launched project — no guessing. Plain (non-tank)
  Maya has no engine context → `"0"` ("no project") so a project-scoped create
  fails loudly instead of writing to a stale `.env` default. Mirrors fpt-mcp's
  console project-resolution. New `tests/test_project_context.py` (7 tests).

## [1.14.0] — 2026-06-22

### Added
- **Native Toolkit publish** — `maya_session(action="publish")` drives the
  `tk-multi-publish2` PublishManager inside an engine'd (tank-launched) Maya:
  `preview` reads the collected publish tree, `publish` runs
  validate → publish → finalize, and dependencies are captured automatically by
  the plugins. +19 offline tests. (PR #12)
- **Reasoning-effort selector in the Console panel** — header combo
  (Auto / Low / Medium / High / Max, default **Auto**) controlling the spawned
  `claude` subprocess effort via `build_backend_env`; affects only the
  MCP-spawned subprocess, never the user's top-level session. (PR #14)

### Changed
- **Console default model → Claude Opus 4.8** (Fable 5 kept as a selectable
  option, Sonnet 4.6 retained). (PR #13)
- **Console panel colour scheme → Autodesk palette** — neutral grayscale base
  with Autodesk yellow (`#ffff00`) for accents, titles and primary buttons
  (dark `#1c1c1c` text on yellow); the previous blue/slate base and cyan accent
  are retired. Status colours (green / red) unchanged.

## [1.13.0] — 2026-06-15

### Added
- **`maya_session(action="operation_history")`** — a read-only companion to the
  durable audit log (`_audit.py` / `MAYA_AUDIT_LOG`), which was previously
  write-only (`jq`/`grep` only). New `_audit.read_records(log_path, *, limit,
  tool, action, status)` returns recent records **newest-first**, spans the
  rotated `.1` sibling, and mirrors the substrate's best-effort contract (missing
  file → `[]`, malformed line skipped, never raises). The action returns an
  explanatory payload (not an error) when the log is OFF, so the caller knows to
  set `MAYA_AUDIT_LOG=1`. It is a **dispatcher action, not a new tool** — the
  tool count stays **15** — and a pure file read (no Command Port traffic,
  nothing on Maya's main thread). Excluded from self-auditing. Resolves the
  Chat 66 P3 follow-up. +14 tests in `tests/test_audit.py`.
- **Shared OPSEC error sanitisation** (`error_scrub.py`) — `server._handle_error`
  now scrubs credential-shaped tokens and length-bounds (300 chars) the
  exception text it returns to the model, via the byte-identical ecosystem helper
  (canonical `~/Projects/error_scrub_canonical.py`; same copy in fpt-mcp /
  flame-mcp). The `Maya error` / `Unexpected error` prefixes are preserved so
  `_audit.status_from_output` still classifies failures. +10 tests
  (`tests/test_error_scrub.py`).

### CI / Docs
- **Code knowledge graph auto-publishes to GitHub Pages** on push to `src/**`
  (`.github/workflows/graphify-pages.yml` + `scripts/graphify/`), original
  force-directed layout + deterministic file-based community names (no LLM key);
  README links the live graph. `src/graphify-out/` is gitignored.

## [1.12.0] — 2026-06-15

### Added

- **Durable, append-only audit log of tool executions (opt-in, OFF by default)** —
  a new `src/maya_mcp/_audit.py` module writes a forensic/accountability record
  to `src/maya_mcp/logs/audit.jsonl`, distinct from the F0 efficiency stream in
  `logs/timings.jsonl`. Enabled only when the **`MAYA_AUDIT_LOG`** env var is set
  to `1`/`true`/`yes`/`on`; unset/empty is a no-op (no file, no perf/disk/privacy
  impact, no behaviour change). Each entry records `ts`, `tool`, `action`,
  sanitised `params`, `status` (`ok` / `error` / `safety_blocked` /
  `ast_rejected`), and `model`/`backend`. For `execute_python` the code is stored
  **truncated to ~2000 chars plus a SHA-256 of the full code** and its length;
  Maya result payloads are never stored. Wired at the `maya_session` dispatcher,
  on the standalone mutation tools (a one-line `@_audited` decorator), and inside
  `_do_execute_python` / `_do_delete` so safety/AST **blocked** attempts are
  captured; read-only actions (`ping`, `list_scene`, `scene_snapshot`) are
  excluded. Persistence reuses `_session_stats.persist_timing` (5 MB + `.1`
  rotation, best-effort — an audit failure never breaks a tool call). Write-only:
  **no new MCP tool** (the 15-tool count is unchanged); inspect it with
  `jq`/`grep`. Documented in `README.md`, `.env.example`, and `CLAUDE.md` §6.

### Fixed

- **`_meta.plugins_loaded` no longer lists `mtoa` twice** — the introspector
  concatenated the default `_PIPELINE_PLUGINS` (which includes `mtoa`) with the
  `MAYA_INTROSPECT_PLUGINS` env var (commonly also `mtoa`), recording the
  plugin twice. `scripts/introspect_maya_api.py` now de-duplicates the plugin
  list preserving order, and the committed `api_graph.json` `_meta` was edited
  to drop the duplicate. Cosmetic only — the 4851-command set is unchanged.

## [1.11.0] — 2026-06-15

### Changed

- **12 synchronous bridge calls moved off the asyncio event loop** — twelve
  `bridge.execute(...)` / `bridge.ping()` calls inside `async def` tools ran
  synchronously, blocking the event loop for the whole Command-Port round trip
  (the audit's nice-to-have; e.g. `_do_launch`'s up-to-10s ping). Each is now
  `await asyncio.to_thread(...)`, matching the pattern the heavy tools
  (`maya_mesh_operation`, `execute_python`, …) already used. Behaviour-
  preserving: validated in-vivo against live Maya 2027 — `bridge.execute`
  sync vs `to_thread` returns byte-identical results, and a refactored
  `_do_ping` reports correctly. The one-time, non-critical panel-setup call is
  left synchronous by design.

- **`api_graph.json` regenerated with Arnold (mtoa) loaded** — the graph was
  previously introspected without mtoa (plugins: Abc/fbx/USD/obj only), so
  `arnoldRender` and the other Arnold commands were absent and F4b AST
  validation rejected them. Re-introspected headless via `mayapy` (Maya 2027 +
  mtoa 7.5.1.1, `MAYA_INTROSPECT_PLUGINS=mtoa`): now 4851 commands (was 4831)
  including `arnoldRender`. The curated mtoa allowlist stays as a belt-and-
  suspenders for commands behind plugins not installed on every box.

### Security

- **Command Port bound to `localhost` instead of all interfaces** — both the
  installer (`install.sh` Step 7 `build_block`) and the runtime auto-injector
  (`server.py::_inject_user_setup`) opened the Maya Command Port with the bare
  `":8100"` form, which binds to every network interface. Because the bridge
  speaks MEL (`sourceType="mel"`) and MEL `python(...)` runs arbitrary Python,
  this exposed an unauthenticated arbitrary-code-execution port to the whole
  LAN on every artist workstation. Both injectors now bind
  `"localhost:8100"`. `BLOCK_VERSION` was bumped `2 → 3` so the install-time
  version gate rewrites already-provisioned `userSetup.py` blocks, and the
  runtime injector's "healthy block" check now requires the `localhost:` form
  so stale all-interfaces blocks are regenerated on the next connect.
- **Vision3D HTTP client now verifies TLS by default** — `GPU_VERIFY_TLS`
  defaults to `true` (was `false`). Plain-`http` LAN targets are unaffected
  (httpx ignores the flag for non-TLS URLs); set `GPU_VERIFY_TLS=false` to opt
  out for self-signed `https` endpoints.

### Fixed

- **Injection / `SyntaxError` on free-form paths and names in the dedicated
  Maya tools** — `maya_import_file`, `maya_viewport_capture`,
  `maya_create_primitive`, `maya_assign_material`, `maya_transform`,
  `maya_create_light`, `maya_create_camera`, `maya_mesh_operation`,
  `maya_set_keyframe`, `maya_session(delete/list_scene/shelf_button)` built
  Maya Python by raw f-string interpolation of user-supplied paths/names into
  single-quoted literals, bypassing `check_dangerous`. A legitimate value
  containing a quote (e.g. `Director's_cut/char.obj`) raised a `SyntaxError`;
  a crafted value could inject code. A new `_py_str()` helper now wraps every
  free-form interpolation as an escaped Python literal via `repr()`.
- **Arnold commands no longer statically rejected out of the box** — the
  committed `api_graph.json` was introspected without the `mtoa` plugin, so the
  F4b AST validator rejected every corpus-recommended `cmds.arnoldRender(...)`
  call. A curated `_MTOA_COMMANDS` allowlist is now folded into the validator's
  valid set (only when a real graph is loaded, so the empty-graph no-op is
  preserved), and `scripts/introspect_maya_api.py` now loads `mtoa` by default
  so a future live regeneration carries the Arnold command surface natively.
- **`maya_session(new_scene)` no longer silently discards unsaved work** — it
  now checks `cmds.file(q=True, modified=True)` and refuses with an
  `unsaved_changes` error unless called with `{"confirm": true}`.
- **`_build_quality_form_data` dead `if/elif`** — both branches were
  identical, so `target_faces=0` (the `ShapeTextInput` default) was always sent
  to the Vision3D API, pinning the server to 0 instead of letting it apply its
  own default. Collapsed to `if params.target_faces > 0`, matching the
  `octree_resolution` / `num_inference_steps` guards.

### Changed

- **All 22 Pydantic input models now set `extra="forbid"`** (including
  `SearchMayaDocsInput` and `LearnPatternInput`, which previously had no
  `model_config`), so a misspelled parameter name raises instead of being
  silently ignored.
- **`learn_pattern` return payload is explicit about searchability** — it now
  reports `"searchable": false` and states that the ChromaDB index and BM25
  `corpus.json` must be rebuilt (`python -m maya_mcp.rag.build_index`) before a
  learned pattern appears in `search_maya_docs` results.
- **`maya_rag.log` is now size-capped** (5 MB, one `.1` rollover), matching the
  existing `persist_timing` telemetry rotation, so no append-only log grows
  unbounded.
- **`src/maya_mcp/rag/candidates.json`** (the `learn_pattern` staging file for
  non-trusted models) is now `.gitignore`d so model-authored candidate code
  cannot be folded into history by `git add -A`.
- Documentation drift corrected: tool-count strings outside concept anchors
  (`README.md`, `CLAUDE.md` diagram, `install.sh` messages/comments) now read
  **15 MCP tools**; the `server.py` module docstring reads **7 Vision3D
  actions**; `docs/DEPLOY.md` manual Command Port snippets use
  `commandPort(name="localhost:8100", sourceType="mel")`; the CHANGELOG
  reference-link footer is complete through `v1.10.1`; and the
  `.pre-commit-config.yaml` header is relabelled from `flame-mcp` to
  `maya-mcp`.

### Tests

- Added a `_model_can_write()` trust-gate test (the corpus-poisoning guard) and
  Arnold-allowlist validator tests; updated the install-block tests to assert
  the `localhost:` Command Port bind.

## [1.10.1] — 2026-06-10

### Fixed
- **pytest clobbered the developer's REAL `userSetup.py`** —
  `tests/test_install_usersetup.py` exec's the install.sh Step 7 heredoc as a
  module at import time; the heredoc ended with a bare `sys.exit(main())`, so
  `main()` ran on every pytest collection with the `/fake/repo/root` argv
  fixture and wrote that fake root into
  `~/Library/Preferences/Autodesk/maya/<ver>/scripts/userSetup.py`. The
  runtime injector then repaired it on every fresh server connect — the cause
  of the recurring `[MCP] userSetup.py updated` warning in Maya. Step 7 now
  has an `if __name__ == "__main__":` guard (installer behavior unchanged);
  a regression test locks the guard in place.
- **userSetup.py writer ping-pong** — the runtime injector
  (`_setup_maya_panel`) and install.sh Step 7 share sentinels but emit
  different block formats, so each rewrote the other's block (one warning per
  fresh server process). The injector now respects any existing healthy block
  (correct repo root + port) regardless of which writer produced it.

## [1.10.0] — 2026-06-10

### Added
- **Visible-progress streaming (Chat 62 design, MCP-native)** — long-running
  paths now stream progress to MCP clients via FastMCP `Context` instead of
  staying silent: `maya_session(action="launch")` emits `ctx.report_progress`
  every poll plus `ctx.info` lines while waiting for the Command Port (validated
  in-vivo: fresh Maya 2027 launch, 6 progress events, ready in 18s);
  `maya_session(action="execute_python")` and `maya_import_file` run the bridge
  call in a worker thread with a 10s `ctx.info` heartbeat
  (`_execute_with_heartbeat`). Fast operations emit nothing — no noise on the
  common path. `ctx` is optional everywhere, so direct calls and test doubles
  keep working.
- **Per-call Command Port timeout** — the bridge socket timeout (fixed 10s
  instance default) can now be raised per call: `execute_python` accepts
  `{"timeout": 1-600}` and `maya_import_file` uses a 120s budget. Previously
  ANY operation that kept Maya's main thread busy past 10s returned a
  guaranteed `MayaConnectionError` while Maya silently kept executing it
  (documented Chat 57/58 gotcha); now long operations wait with heartbeats and
  deliver their real result (validated in-vivo: 12s busy-loop with
  `timeout: 20` → heartbeats + correct result).

## [1.9.2] — 2026-06-10

### Changed
- **Cloud model selector refreshed** — the Console panel now offers Claude
  Fable 5 (`claude-fable-5`), Claude Opus 4.8 (`claude-opus-4-8`) and Claude
  Sonnet 4.6 (Opus 4.7 removed). Self-learning (`learn_pattern` write-trust) is
  now reserved for **Opus + Fable** — Sonnet and local models are read-only
  (`WRITE_ALLOWED_MODELS`, `config.json` example, README/CLAUDE.md in lockstep).
  `VISION_MODELS` updated to the new cloud IDs.

## [1.9.1] — 2026-05-26

### Fixed
- **install.sh — userSetup.py block idempotency (Chat 55 bug)** — re-running
  install.sh no longer silently skips a stale userSetup.py block that is missing
  the command-port opening line.  Root cause: `upsert_block()` relied solely on
  byte-level content comparison; a block written by an older install (menu-only,
  no port line) that happened to byte-match would be left untouched.
  Fix: introduced a `BLOCK_VERSION` integer (currently `2`) embedded as a comment
  marker (`# MCP Pipeline Console block vN`) on the second line of every managed
  block.  `upsert_block()` now reads that marker first; if it is absent or lower
  than `BLOCK_VERSION`, the block is regenerated unconditionally before the content
  check runs.  The `--doctor` check for `userSetup.py` also reports stale-version
  blocks as FAIL with a remediation sentence.  Added 23 pytest unit tests in
  `tests/test_install_usersetup.py` covering fresh install, same-version no-op,
  stale-block refresh, version-bump regeneration, and the Maya-2027 `name=` kwarg
  regression guard.
- **F4b false-positive on plugin commands (Chat 56 bug)** — `api_graph.json`
  was generated from a headless `maya.standalone` session that loads no plugins,
  so plugin-registered commands (`mayaUSDImport`, `AbcImport`, `FBXImport` …)
  were absent from the graph and the F4b AST validator rejected legitimate calls
  to them before the Command Port round-trip. `scripts/introspect_maya_api.py`
  now best-effort loads the pipeline I/O + USD plugins (`mayaUsdPlugin`,
  `AbcImport`, `AbcExport`, `fbxmaya`, `objExport`) before walking `dir(cmds)`,
  records the loaded/failed sets in `_meta`, and accepts extra plugins via the
  `MAYA_INTROSPECT_PLUGINS` env var. A plugin absent on the running build is
  skipped, never fatal.

### Changed
- **`api_graph.json` regenerated from Maya 2027** with the pipeline plugins
  loaded: **4831 commands** (was 4608 from Maya 2026 without plugins),
  `maya_version` `2026` → `2027`. F4b now accepts `cmds.mayaUSDImport(...)` &
  siblings while still rejecting hallucinations (`cmds.polyCubez` → suggests
  `polyCube`).

## [1.9.0] — 2026-05-21

### Added
- **F0 session-stats telemetry (3C Wave 2)** — new `src/maya_mcp/_session_stats.py`
  module: `persist_timing`/`persist_turn` JSONL streams with 5 MB rotation,
  30-minute idle auto-reset, and the `turns_total`/`failed_turns` counters that
  drive `p_fallo = failed_turns / turns_total` over the `execute_python` path
  (the error-prone free-form path). `session_stats` now reports `p_fallo`; new
  `reset_session_stats` tool (tool inventory 14 → 15). Cross-session timing
  baselines persist to `logs/timings.jsonl`. New `stats_keys_schema_shared`
  concept invariant locks `_stats` to `make_empty_stats()`. Ported from
  flame-mcp for ecosystem parity.
- **Golden RAG regression dataset (3C Wave 3)** — `tests/golden/maya_queries.jsonl`
  (40 queries, 16 adversarial) + `tests/test_golden.py`.
- **Ollama `keep_alive` 30 m + `config.json` knob (3C Wave 1)**.

### Changed
- **Trimmed `CLAUDE.md` operator sections → `docs/DEPLOY.md` (3C Wave 5)** so the
  LLM system prompt no longer carries install/deploy shell recipes.

## [1.8.2] — 2026-04-28

### Fixed
- `src/maya_mcp/server.py` — four pre-existing bugs surfaced by the Chat 49
  in-vivo validation against a live Maya 2026 session. None were introduced
  by recent work; all were dormant because typical flows omit the parameters
  that triggered them.
  - `maya_create_primitive` with `name=`: generated `cmds.polySphere(, name='X')[0]`
    — leading comma with no first positional argument → `SyntaxError` on every
    call that passed a name. Fix: drop leading comma in `name_arg`.
  - `maya_create_camera` with `name=`: identical pattern, identical
    `SyntaxError`, identical fix.
  - `maya_create_light` with `name=` (non-area types): identical pattern.
    Area lights spared because `cmds.shadingNode('areaLight', asLight=True,
    name='X')` already has a preceding kwarg, so the comma is correct. Fix:
    bare `name_kw` + conditional `, ` prefix only at the area call site.
  - `maya_viewport_capture` with `camera=`: f-string substitution does not
    preserve enclosing indentation, so the multi-line `camera_opt` block
    landed unindented inside `try:` and produced `IndentationError: expected
    an indented block after 'try' statement on line 5`. Fix: move the
    camera switch out of the try (it is a display change, not a scene-state
    change, so it does not need `undoInfo` rollback).

### Documentation
- `docstring(maya_import_file)` clarification: the GLB native-parser path
  (`type='glTF Import'`) requires Maya 2027+ where the `libgltfsceneimport`
  plugin ships by default. Maya 2026 does NOT have this plugin (validated
  in Chat 49 against a live Maya 2026.x session — only `gameFbxExporter`
  and `fbxmaya` were registered). The v1.8.1 changelog entry for `a2446b1`
  said "Maya 2026 native glTF" which is incorrect — it's 2027+. The OBJ
  fallback (`mesh_uv.obj` + `texture_baked.png` siblings) IS exercised on
  Maya 2026 when those files are present, which is the case for Vision3D
  paint-pipeline output but not for shape-only output.

## [1.8.1] — 2026-04-28

### Fixed
- `src/maya_mcp/maya_bridge.py` — `send_python()` no longer races against
  the `/tmp` wrapper file. The bridge previously wrote a wrapper `.py` to
  `/tmp`, asked Maya to `exec()` it, then deleted it in a `finally` block.
  If the bridge timed out while Maya was busy (Toolkit init, modal dialog,
  USD export), the `finally` block could delete the wrapper before Maya
  actually read it, producing `FileNotFoundError: /tmp/_mcp_wrap_*.py`. The
  new path base64-encodes the entire wrapper (user code + result writer)
  and inlines it directly in the MEL `python()` call. No wrapper file
  exists to race against. The only remaining temp file is `_MCP_RESULT_PATH`
  which Maya writes — it cannot go missing before the bridge polls for it.
  Also removes the now-unused `_prepare_wrapper_files()` static method.
  269 tests pass. (commit `1e22189`)
- `src/maya_mcp/server.py` — `maya_import_file` now correctly handles GLB
  imports. Loads the `libgltfsceneimport` plugin before invoking the
  importer (Maya 2026 native glTF), uses `type='glTF Import'`
  (case-sensitive — `'GLTF Import'` silently fails), and falls back to
  `mesh_uv.obj + aiStandardSurface + texture_baked.png` when GLB import
  fails. `src/maya_mcp/suggestions.py` updated so the vision3d download
  chaining hint points to `maya_import_file` instead of `execute_python`.
  (commit `a2446b1`)
- `src/maya_mcp/server.py` — `_inject_user_setup()` no longer rewrites
  `userSetup.py` on every MCP panel setup call. Each new `claude -p`
  subprocess resets `_panel_setup_done` to False, so the server-level
  guard didn't help and the spurious `[MCP] userSetup.py updated`
  warning appeared 3× per session. The function now compares computed
  `new_content` against the existing file and returns early when they
  match. (commit `399cfe6`)

## [1.8.0] — 2026-04-22

### Added
- `src/maya_mcp/suggestions.py` — two new chaining rules:
  - `maya_create_camera → maya_viewport_capture`: seeds
    `camera=<name>` + `output_path=/tmp/<cam>_preview.png` for the
    "did my framing land where I wanted?" feedback loop in shot
    layout work.
  - `maya_create_light → maya_set_keyframe`: seeds the initial
    intensity keyframe (value=1.0, frame=1) for the new light, as
    animation groundwork. Users typically follow up with another
    frame at a later time for the actual interpolation.
  Tests grew from 258 to 267 (+9); registry grew from 3 to 5 rules.
- `scripts/invariant_types.py` — `_write_subset` handler registered
  in WRITERS (Phase C + D, Chat 48). Covers `b_source.type:
  anchor_list` (without `item_pattern`) and `file_regex_matches`
  (with YAML opt-in `b_source.writer.line_template`). Enables
  `/propagate-change` Path A to auto-fix subset-drift without
  manual edits for the common cases.
- `.github/workflows/ci.yml` — Codecov coverage upload step
  (`codecov/codecov-action@v4`), gated to `matrix.python-version ==
  '3.12'`.

### Fixed
- `scripts/invariant_types.py` — `version_match` handler honors
  opt-in `tolerate_release_in_progress: true`. Applied to
  `.concepts.yml` on the `pyproject_matches_latest_tag` invariant
  to unblock `cut-release.sh` under strict mode.

## [1.7.0] — 2026-04-22

### Added
- `src/maya_mcp/suggestions.py` — next_suggested_actions pattern port
  (Chat 47). JSON-mutate contract same as fpt-mcp. First rule: 3-branch
  Vision3D chain (`generate_image/text → poll → download →
  maya_session import`). Kill switch `MAYA_MCP_DISABLE_SUGGESTIONS=1`.
  Wired via `maybe_annotate_with_suggestions("maya_vision3d", …)` in
  `server.py`.
- `.concepts.yml` — `next_suggested_actions_contract` concept with
  `every_rule_is_wired` invariant. Pre-commit fails if a rule is
  registered without wiring at the tool level.
- `src/maya_mcp/suggestions.py` — two new chaining rules (Chat 48,
  this release): `maya_create_primitive → maya_assign_material` (seeds
  `object_name` + `aiStandardSurface` default, whitelists the 6
  primitive kinds) and `maya_import_file → maya_session(save_scene)`
  (fires only on `imported > 0`, singular/plural reason text). Tests
  grew from 244 to 258 (+14); invariant count 24 → 27.
- `.github/workflows/ci.yml` — GitHub Actions CI workflow. Four blocking
  jobs: pytest across Python 3.10/3.11/3.12 (Qt forced to
  `QT_QPA_PLATFORM=offscreen`), ruff lint, mypy, verify_concepts.
  Pytest coverage reported inline.
- `.github/workflows/pr-review.yml` — automated Claude PR review
  (`anthropics/claude-code-action@v1`). Byte-identical across the 4
  ecosystem repos. Uses `claude_code_oauth_token`. Requires the
  Claude Code GitHub App installed on the repo + workflow permission
  `id-token: write` + `--model claude-sonnet-4-6` pin so the OAuth
  token (Sonnet-scoped on Max/Pro) works against the default-Opus
  action.
- `scripts/verify_concepts.py --write` — WRITER MODE (Chat 46).
  Requires the triple flag `--accept-current-as-truth
  --i-reviewed-diff --write`. Dispatches to per-type writers in
  `invariant_types.py::WRITERS`. Currently supports `tool_count` and
  `review_expiry`; other types report `WRITER UNSUPPORTED`. No
  auto-commit.
- `scripts/invariant_types.py` — `ast_dict_keys` canonical (Chat 47)
  now reads `ast.AnnAssign` in addition to `ast.Assign`. Synced
  byte-identical across 4 repos.
- `scripts/invariant_types.py` — `version_match` canonical (Chat 48)
  honors opt-in `tolerate_release_in_progress: true`. Lets
  `cut-release.sh` commit a version bump before the matching git
  tag exists under strict mode.
- `scripts/verify_concepts.py` — `ci_skip: true` flag on individual
  invariants + auto-skip of `review_expiry` under `GITHUB_ACTIONS`.

### Changed
- `.concepts.yml` — `strict: false → true`. The pre-commit hook now
  blocks commits on any unresolved invariant drift instead of only
  reporting it. Ecosystem-wide flip on 2026-04-20 (Chat 46), unblocked
  by the `changelog_tag_sync` release-in-progress tolerance shipped in
  v1.6.3.
- CI pipeline cleanup (Chat 47): ruff baseline cleared, mypy baseline
  cleared (`[tool.mypy]` block in pyproject), both jobs flipped to
  blocking.

### Fixed
- `.github/workflows/ci.yml` — CI uses `pip install -r
  tests/requirements-test.txt` to pull the full test dependencies
  (Chat 47). The earlier handoff misdiagnosed the first CI failure as
  Qt offscreen; real cause was missing pytest-asyncio.
- `.github/workflows/pr-review.yml` — added `id-token: write` workflow
  permission (Chat 48). Without it the action errored with "Unable to
  get ACTIONS_ID_TOKEN_REQUEST_URL env variable".
- `.github/workflows/pr-review.yml` — pinned `--model claude-sonnet-4-6`
  via `claude_args` (Chat 48). OAuth tokens from `claude setup-token`
  are scoped to Sonnet on Max/Pro; the action's default model (Opus
  after v1.0.100) returned `401 Invalid bearer token` against those
  credentials (see anthropics/claude-code-action#584).

## [1.6.3] — 2026-04-20

### Added
- `scripts/cut-release.sh` — ecosystem-shared release orchestrator. Validates
  clean tree + semver arg + non-empty `[Unreleased]`, edits CHANGELOG +
  pyproject.toml, commits with `CUT_RELEASE_VERSION=X.Y.Z` so the
  `changelog_tag_sync` invariant tolerates the transient pre-commit drift,
  then tags, pushes, and creates a GitHub release with the CHANGELOG
  section as notes. Ships with `--dry-run` for safe previews. Byte-identical
  across the 4 MCP-ecosystem repos; canonical at
  `~/Projects/cut-release-canonical.sh`. Resolves the Chat 45 P1 release-flow
  tension (pre-commit vs release-flow) that was blocking the ecosystem-wide
  `strict: true` flip.
- `scripts/invariant_types.py` — new `changelog_tag_sync` handler replaces
  the previous `subset`-based `changelog_tag_coherence` invariant. Adds
  release-in-progress tolerance anchored to env `CUT_RELEASE_VERSION` (set
  by `cut-release.sh` at commit time) OR `pyproject.toml`'s `version`
  field. The tolerance only fires for exactly one drifting version that
  matches the anchor — you cannot forge it without also bumping the real
  anchor. Handler propagated byte-identical to the 4 MCP-ecosystem repos.

### Fixed
- `MODEL_STRATEGY.md` §2b — the `qwen3.5-mcp` Modelfile snippet still
  declared `PARAMETER num_ctx 8192`, inconsistent with the ecosystem-wide
  bump to `16384` already applied in v1.6.2's `CLAUDE.md` §11 and `README.md`
  fixes. Follow-up that closes Chat 45 Handoff gotcha #5. Since
  `qwen3.5-mcp` is a single Ollama model shared across fpt-mcp and
  maya-mcp, the Modelfile snippet must match the value documented
  elsewhere. Added a one-line pointer to `fpt-mcp/MODEL_STRATEGY.md`
  for the VRAM/headroom rationale (not duplicated here).

## [1.6.2] — 2026-04-20

### Fixed
- `README.md` — the `qwen3.5-mcp` setup block referenced `Modelfile.qwen35mcp`
  as if it existed in the repo, but no Modelfile is tracked here. Replaced
  with an inline heredoc so the documented command works in a fresh clone.
- `CLAUDE.md` §11 — `num_ctx` was documented as `8192`, inconsistent with
  fpt-mcp's ecosystem-wide bump to `16384` (Bucket D). Since `qwen3.5-mcp`
  is a single Ollama model shared across fpt-mcp and maya-mcp on the same
  machine, the docs must agree on the single runtime value. Aligned with
  the ecosystem decision.

### Added
- `scripts/verify_concepts.py` — `--accept-current-as-truth` + `--i-reviewed-diff` double-flag escape hatch (REPORT MODE ONLY). When both flags are passed, the runner inspects every failing invariant and prints a human-readable "would update \<mirror\>" line describing what a hypothetical writer mode would change, then exits 0 without touching any file. Single-flag usage is rejected with exit code 2 by design — the double-flag requirement prevents accidental drift acceptance. Intended for repos that drifted while dormant and need a one-shot review before flipping `strict: true`. Writer mode is deferred to a future pass with explicit user sign-off. Chat 44 ultraplan Q5.

## [1.6.1] — 2026-04-20

Point release fixing silent context-window truncation on Mac-local Ollama and
hardening the ecosystem release hygiene via a new concept invariant.

### Fixed

- **`ollama_mac` num_ctx preflight** (`console/claude_worker.py`). Ollama's
  Anthropic-compatible `/v1/messages` endpoint ignores the Modelfile
  `num_ctx` directive and silently defaults to 4096 tokens, truncating long
  MCP prompts mid-stream with no error. The console worker now POSTs a
  zero-prompt warm-up to `/api/generate` with `options.num_ctx=8192`,
  `keep_alive="10m"`, and `stream=False` immediately before spawning the
  `claude` subprocess on the Mac-local branch. The preflight uses stdlib
  `urllib.request` (no new dependency), times out at 120 s, and is
  non-fatal: network failures are logged and the subprocess spawn
  proceeds. The `ollama` LAN backend (glorfindel) and hypothetical
  `ollama_cloud` backend are deliberately NOT preflighted: LAN runs under
  different Modelfile defaults and cloud runners manage context themselves.

### Added

- **`ollama_preflight_parity`** concept invariant (`.concepts.yml`): grep
  check that `_preload_ollama_mac_model(` appears in `console/claude_worker.py`.
  Prevents silent regression if the preflight call is ever removed.
- **`github_release_per_tag`** ecosystem-wide concept invariant: every git
  tag `vX.Y.Z` with `X >= 1` must have a corresponding published GitHub
  Release. Pre-1.0 tags are excluded (v0.x was pre-release noise). Backfilled
  missing releases for v1.0.0 and v1.1.0 during this release. Requires the
  `gh` CLI authenticated; drifts are false-positive if `gh` is offline or
  unauthenticated.

### Infrastructure

- Invariant count: 24 → 26. All green on HEAD.
- Test count: 223 → 228. New `tests/test_ollama_mac_preflight.py` covers
  the constant, the `urlopen` call shape, and the non-fatal behaviour on
  both `URLError` and generic exceptions.

## [1.6.0] — 2026-04-20

Rolls out Fase B of the ecosystem concept-registry pattern (originating in
flame-mcp Chat 44). Every load-bearing cross-cutting concept — tool count,
dispatcher action sets, Command Port default, Vision3D runtime-only URL
policy, pyproject↔tag sync, CHANGELOG↔tag sync, Anthropic model catalogue
freshness, RAG corpus inventory, release cadence — is now declared in
`.concepts.yml` and machine-checked on every commit via a pre-commit hook.
Also closes three real drifts discovered during the audit.

### Added

- **`.concepts.yml`** — 11 concepts, 24 invariants, strict: false (soft-launch
  for ~2 weeks per ecosystem policy). Captures, among others: 14 `@mcp.tool`
  decorators must match README + CLAUDE.md + `install.sh` TOOLS; the 9-member
  SessionAction enum and 7-member Vision3DAction enum must match their
  README/CLAUDE.md tables bidirectionally; Command Port default stays 8100
  (Chat 41 Flame-collision fix); no `vision3d_servers` field or loader may
  re-enter `src/` or `config.example.json` (Chat 40 per-session-URL design);
  `pyproject.toml` version must match latest annotated git tag; Anthropic
  model IDs in `console/claude_worker.py` must be a subset of the current
  block in `~/Projects/.external_versions.yml` and that block must have
  been reviewed within 14 days.
- **`scripts/invariant_types.py`** — shared type library (originally verbatim
  from flame-mcp). Extended with two backward-compatible source types:
  `ast_decorator_kwarg` (for `@mcp.tool(name="public_name")` patterns where
  the Python function name differs from the public tool name) and
  `ast_enum_values` (for string-valued Enum classes used as dispatch tables).
  Flame-mcp can resync on any future pass and gain the new capabilities
  without behaviour change.
- **`scripts/verify_concepts.py`** — engine (verbatim from flame-mcp).
  Reports 24/24 PASS on HEAD.
- **`.pre-commit-config.yaml`** — local hook runs `verify_concepts.py` on
  every commit. `pre-commit install` wires it into `.git/hooks/pre-commit`.
- **Concept anchors** in `README.md` (`mcp_tool_count`, `mcp_tool_table`,
  `maya_session_actions`, `maya_vision3d_actions`), `CLAUDE.md` (inline
  `mcp_tool_count`, `maya_session_actions`, `maya_vision3d_actions`), and
  `install.sh` (`install_tools_list`).

### Fixed

- **`pyproject.toml` version drift**: bumped from stale `"0.1.0"` (left over
  from the initial scaffold) to `"1.5.0"`, matching `git describe --tags
  --abbrev=0`. Prevents the `pip show maya-mcp` / `pip install -e .` drift
  that hit the sister repo flame-mcp at Chat 43.
- **`install.sh` tool-count message**: the success line printed "27 maya-mcp
  tools pre-approved" while only 14 tools exist in the TOOLS list (stale
  since v1.4.0, when dispatch action names were correctly removed from the
  pre-approval surface). Fixed to "14".
- **Anthropic Opus model drift**: `console/claude_worker.py` had
  `claude-opus-4-6` in `AVAILABLE_MODELS` and `VISION_MODELS`; the ecosystem
  oracle (`~/Projects/.external_versions.yml`, reviewed 2026-04-17) lists
  `claude-opus-4-7` as the current Opus. Updated both references.

### Migration notes

- **Run `pre-commit install`** once per clone (inside the repo venv) to
  wire the hook:
  ```bash
  .venv/bin/pip install pre-commit
  .venv/bin/pre-commit install
  ```
- **When editing any of the source-of-truth files** listed in `.concepts.yml`,
  update the mirrors in the same commit. The pre-commit hook will report
  drift under soft-launch but not block. After the ~2-week soft-launch
  window, `strict: true` will be flipped in a separate commit and drift
  becomes a hard block.
- **Run `python scripts/verify_concepts.py --verbose`** at any time to see
  which invariants are currently failing.

## [1.5.0] — 2026-04-15

Two-half release that closes a family of Chat 41 incidents: the bridge
side silently returned empty for every command under a surprisingly
common set of conditions, and the install side stopped short of
actually configuring Maya — together producing a failure class where
`maya_ping` reported `connected` with all fields blank and no trail
back to the root cause.

### Added

- **File-based `send_python` return mechanism.** The wrapper now
  writes its stringified result to a uuid-named file in `/tmp` and the
  bridge reads that file locally, replacing the old dual-connection
  pattern that relied on Maya's command port echoing the stdout of
  `python("print(_mcp_result)")`. Completely removes the dependency
  on `echoOutput=True` and fixes the Chat 41 scenario where every
  `execute_python` returned empty string while `maya_ping` kept
  working (because ping uses `send_mel` directly). Single TCP
  connection per invocation, no module-global pollution, uuid paths
  prevent collision under concurrent calls. (Commit `c4f3a79`.)
- **`MayaConnectionError` raised on empty recv timeout.** `_send_raw`
  now tracks whether `recv()` ever returned (with data or a clean
  close). A `socket.timeout` with an empty buffer raises
  `MayaConnectionError` with a diagnostic listing the three known
  causes (modal dialog / long-running command / orphaned port after
  a crash) instead of silently returning an empty string that the
  caller would misinterpret as a successful no-op. Fixes the
  false-positive cascade where `ping()` returned
  `status='connected'` with empty version, which led `_do_launch` to
  enter `already_running`. (Commit `7de791e`.)
- **Install automation Step 7.** `install.sh` now detects every Maya
  version installed on the host (macOS app bundles under
  `/Applications/Autodesk/maya*/Maya.app`, Linux installs under
  `/usr/autodesk/maya*-x64`) and writes an idempotent guarded block
  into each version's user `scripts/userSetup.py`. The block adds
  the repo root to `sys.path`, opens the Command Port on `MAYA_PORT`
  (from `.env`, default `8100`) in `mel` mode with the `name=`
  kwarg form (Maya 2027 silently ignores the positional form when
  `sourceType` is specified), and registers the MCP Pipeline menu
  via `executeDeferred`. Detection trusts only application-binary
  evidence — preference directories left behind by uninstalled Maya
  versions are ignored. Reruns are safe: sentinel markers bound the
  block and the installer replaces the whole region on upsert while
  preserving any user content before or after. (Commit `486ce3e`.)
- **`./install.sh --doctor` subcommand.** Five-check sanity sweep:
  `~/.claude.json` has `mcpServers.maya-mcp` with valid cwd, `.env`
  exists without unexpanded placeholders, `userSetup.py` bootstrap
  is present per detected Maya version, Maya Command Port TCP probe
  returns real data (with specific diagnostics for the Chat 41
  silent-cascade, python vs mel sourceType mismatch, and Flame port
  collision symptoms), and `maya_mcp.maya_bridge` imports cleanly
  from the venv. Each check reports PASS/FAIL/WARN/SKIP with a
  concrete remediation sentence. Exit 0 on PASS/WARN/SKIP, 1 on any
  FAIL — designed so future Claude Code sessions can run the doctor
  as Phase 0 verification before invoking any Maya MCP tool.
  (Commit `55b5e4c`.)
- **`cmds` and `json` preloaded into `send_python` user namespace.**
  The wrapper now populates `_mcp_result_ns` with
  `{'cmds': _mcp_cmds, 'json': _mcp_json}` before exec, so direct
  callers of `bridge.execute("result = cmds.ls()")` no longer have
  to redundantly import `maya.cmds` themselves. Previously the
  wrapper imported `cmds` at its own module level, but since exec
  does not inherit caller globals when an explicit namespace dict
  is passed, the import was dead code — every `server.py` tool
  was working around it by importing `cmds` at the top of its
  generated code, and direct bridge users got a `NameError`. Fix
  discovered during Chat 41 end-to-end smoke testing against live
  Maya 2027. (Commit `5308bee`.)
- **19 new bridge tests** in `tests/test_maya_bridge.py`
  distributed across three regression classes:
  `TestFileBasedReturn` (12 tests covering single-connection
  guarantee, JSON roundtrip, ERROR-prefix raising, missing-file
  diagnostic with the recovery snippet, the Chat 41
  silent-echoOutput scenario, cleanup on success/error/missing,
  uuid uniqueness, and a structural wrapper body guard),
  `TestSilentMayaRecvTimeout` (7 tests covering silent-Maya
  raising, diagnostic message, the data-then-timeout regression
  guard, clean close with empty payload, and end-to-end `ping` /
  `execute` raise paths), plus 2 tests for the `cmds`/`json`
  preload fix. Suite total: 196 → 217. (Commits `c4f3a79`,
  `7de791e`, `5308bee`.)

### Changed

- **Default Command Port moved from 7001 to 8100.** Port 7001 is the
  Maya commandPort convention but collides with Autodesk Flame's
  S+W Service Discovery Multicast port and S+W Probe Server port.
  On hosts with Flame installed (every maya-mcp user who is also a
  Flame artist), a TCP connection to `localhost:7001` silently
  succeeds against Flame's S+W service instead of Maya. Flame
  accepts the connection, returns empty bytes and closes, which
  the pre-v1.5.0 bridge interpreted as a successful no-op. Port
  8100 is adjacent to the existing fpt-mcp cluster (8000, 8090),
  not registered by IANA for anything active on macOS, and far
  from the congested dev ranges (8080, 8443, 8888, 9000). Users
  who still want 7001 can override via `MAYA_PORT` in `.env`.
  (Commit `75faf17`.)
- **`install.sh` renumbered from 6 steps to 7** and its "next steps"
  summary updated to drop the manual userSetup.py bullet (now Step
  7) and point at `./install.sh --doctor` for post-install
  verification. README Installation Step 4 rewritten: automatic now,
  with the manual snippet preserved inside a collapsible fallback
  block for exotic layouts. (Commits `486ce3e`, `55b5e4c`.)
- **Bridge error diagnostic hints** (`_RESULT_FILE_MISSING_HINT`
  in `maya_bridge.py`, the launch hint in `server.py`) corrected to
  point at `sourceType='mel'` without `echoOutput=True` — the
  bridge sends MEL and wraps Python in MEL `python(...)` calls, so
  the port must be in mel mode, and with the file-based return
  `echoOutput` is no longer needed at all. (Commit `486ce3e`.)

### Fixed

- **Chat 41 root-cause cascade.** The combined effect of the bridge
  file-based return, the silent-recv raise, the 7001→8100 port
  migration, the install.sh Step 7 automation, the `cmds`/`json`
  preload, and the sourceType hint corrections is that a user who
  clones maya-mcp, runs `./install.sh`, copies `.env.example` to
  `.env`, restarts Maya once, and restarts Claude Code can now
  invoke `maya_ping` and get real data back without ever editing
  a Maya file by hand. Before v1.5.0, the documented install path
  ended with "add the Command Port snippet to your Maya
  `userSetup.py` (see README.md → Installation → Step 4)", and any
  user who skipped that step ended up with a registered-but-
  non-functional maya-mcp whose failure mode was silent empty
  responses from every tool except `ping`. That class of failure
  is now either automatically avoided (Step 7) or surfaced as a
  `MayaConnectionError` with an actionable diagnostic.
- **`test_send_python_raises_when_result_file_missing`** updated to
  assert the diagnostic points at `sourceType='mel'` instead of the
  now-deleted `echoOutput=True` suggestion. (Commit `486ce3e`.)

### Migration notes

- **No action required** for users on `v1.4.x` who do not have
  Flame installed: port 7001 will still work, the bridge just no
  longer defaults to it. Override via `MAYA_PORT=7001` in `.env`
  if you cannot change your existing Maya `userSetup.py` snippet.
- **Rerun `./install.sh`** once: Step 7 writes the
  `userSetup.py` bootstrap to every detected Maya version. The
  installer is idempotent — reruns on up-to-date hosts report
  `unchanged` and make no file changes.
- **Restart Maya once** after running the installer: Maya picks up
  the new `userSetup.py` at startup, opens the Command Port in the
  right mode, and installs the MCP Pipeline menu automatically.
- **Run `./install.sh --doctor`** after the Maya restart to verify
  all 5 checks are PASS. If any check is FAIL, its message tells
  you exactly what to fix.
- **Direct `bridge.execute("result = cmds.ls()")` callers** no
  longer need to prefix their code with `import maya.cmds as cmds`.
  The preload is backward-compatible: existing callers that already
  import it work unchanged.

## [1.4.0] — 2026-04-12

First tagged release that is documented in this file. Covers every commit
between `v1.3.0` and this tag. The headline feature is the per-session
Vision3D URL selector; several install/docs fixes and a repo-structure
cleanup also ship in this window.

### Added

- **Per-session Vision3D URL selector** (`maya_vision3d` action `select_server`).
  The URL of the Vision3D server is now asked from the user at runtime on
  the first Vision3D call of each MCP session and cached in process memory
  until the MCP server restarts. No URL is persisted to disk anywhere: no
  `vision3d_servers` config field, no hardcoded defaults, no whitelist of
  candidate hosts. Any valid `http://` or `https://` URL typed into the
  chat is accepted (validated via `urllib.parse.urlparse`). The
  `GPU_API_URL` environment variable, if set, is surfaced as a
  `suggested_default` inside the `vision3d_url_required` error payload but
  is never auto-selected — the user still has to confirm or override it
  explicitly. Implementation lives in `src/maya_mcp/server.py` via
  `_resolve_client_or_error()`, `_vision3d_url_required_error()`,
  `_is_valid_http_url()`, and the `_do_v3d_select_server` handler.
  (Commits `688c226`, `3194e81`, `f4756f8`.)
- **Per-URL `httpx.AsyncClient` cache** (`_http_clients: dict[str, AsyncClient]`)
  so switching Vision3D targets mid-session via `select_server` creates a
  new client on demand without rebuilding the existing ones. (`f4756f8`.)
- **`CHANGELOG.md`** (this file). (`v1.4.0` tag.)
- **21 new tests** in `tests/test_vision3d.py` (class `TestVision3dUrlSelection`)
  covering URL validation, resolver states (unselected / selected / switch),
  `select_server` freeform acceptance + malformed rejection + trailing-slash
  normalisation, and the unselected-handler → `vision3d_url_required`
  end-to-end flow. Total suite: 196/196 passing (was 174). (`f4756f8`.)

### Changed

- **`install.sh` pre-approved `TOOLS` list** is now the canonical list of
  MCP-visible tools and exactly matches the 14 `@mcp.tool`-decorated
  functions in `src/maya_mcp/server.py`. Previously, the list mixed in
  action names of the `maya_session` and `maya_vision3d` dispatch tools
  as if they were standalone tools (e.g. `maya_launch`, `maya_ping`,
  `vision3d_health`, `shape_generate_remote`, …), and omitted the
  dispatch tool names themselves — meaning users never got the real
  dispatch surfaces pre-approved and were prompted on first use of every
  action. (Commits `3194e81`, `e81fe96`.)
- **`install.sh`** gained an explicit torch install step (Step 5a) that
  pins `torch==2.6.0` with the correct `+cu124` wheel on CUDA hosts and
  the vanilla wheel on MPS hosts, idempotent against an already-installed
  torch version. `requirements.txt` documents why torch is intentionally
  absent from the pinned dependency list. (Inherited from earlier
  in-window work; see `d3c8399`, `7c7369c` for the rule documentation.)
- **Vision3D dispatch docstring** rewritten to describe the runtime URL
  flow (Step 0: ask the user → `select_server` → proceed). (`f4756f8`.)
- **`CLAUDE.md` and `README.md`** rewritten around the new per-session
  policy. Concrete hostnames removed from every documented example;
  placeholders (`<your-gpu-host>`) used instead. Tool-count claims
  updated (27 → 14 MCP tools, 6 → 7 Vision3D actions). Vision3D action
  table rewritten. `.env.example` guidance updated. (`f4756f8`, `e81fe96`.)
- **`config.example.json`** cleaned: no Vision3D endpoint fields of any
  kind. Only backend/model/Ollama settings. (`f4756f8`.)
- **`.gitignore`** now explicitly protects `src/maya_mcp/config.json` as
  a per-user runtime file, with a header comment documenting that
  Vision3D endpoints are NOT stored in it. (`3194e81`.)
- **`HANDOFF.md`** "Estado actual" and "Relación con vision3d" sections
  resynced to the current code. Historical "Sesión N" session blocks
  preserved verbatim as dated snapshots. (`e81fe96`.)
- **`.env.example` and `.gitignore`**: stale `core/` references cleaned
  up after the `src/maya_mcp/` package layout migration. (`b42362c`.)
- **`.mcp.json`** `flame` entry updated to `-m flame_mcp.server`. (`84ff334`.)
- **`claude_worker.py`**: internal `core/` refs updated to `src/maya_mcp/`.
  (`ea99c76`.)

### Fixed

- **Hardcoded URL defaults removed from code.** `_load_vision3d_servers`
  (present briefly between `688c226` and `3194e81`) was replaced by a
  design that never fabricates a localhost default — if no URL is
  selected and `GPU_API_URL` is unset, the dispatch returns
  `vision3d_url_required` instead of silently aiming the client at
  `http://localhost:8000`. (`3194e81`, superseded by `f4756f8`.)
- **`test_cli_not_found` for `flame_wiretap_tree`** (in the sister
  repo `flame-mcp`): not fixed in maya-mcp, noted here because it was
  flagged during this cycle. See `flame-mcp@81e98ad` for the fix.

### Removed

- **`Vision3DAction.LIST_SERVERS` action** and its handler
  `_do_v3d_list_servers`. Its semantics implied a persistent pool of
  candidate servers, which no longer exists. Vision3D went from 8
  actions to 7. (`f4756f8`.)
- **`_load_vision3d_servers()` function**, the `_vision3d_servers`
  module-level cache, and the associated config loader tests. The
  interim `config.json → vision3d_servers` field (introduced briefly
  in `688c226` and refined in `3194e81`) is gone entirely. No data
  migration is needed because the field only existed during a single
  afternoon and was never shipped in a tagged release. (`f4756f8`.)

### Migration notes

- **No action required** if you were on `v1.3.0` with the old
  `GPU_API_URL` env var: `v1.4.0` still honours it, but as a
  *suggested default* that you confirm via `select_server`, not as
  an auto-selected target. Claude will prompt you in the chat on
  the first Vision3D call of each session.
- If you were using the interim `vision3d_servers` field in
  `config.json` that briefly existed during development (not in any
  released tag), remove it — it is silently ignored in `v1.4.0` and
  may be rejected by future validation.
- `install.sh` should be re-run so the corrected `TOOLS` pre-approval
  list updates your `~/.claude/settings.json`.

[Unreleased]: https://github.com/abrahamADSK/maya-mcp/compare/v1.10.1...HEAD
[1.10.1]: https://github.com/abrahamADSK/maya-mcp/compare/v1.10.0...v1.10.1
[1.10.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.9.2...v1.10.0
[1.9.2]: https://github.com/abrahamADSK/maya-mcp/compare/v1.9.1...v1.9.2
[1.9.1]: https://github.com/abrahamADSK/maya-mcp/compare/v1.9.0...v1.9.1
[1.9.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.8.2...v1.9.0
[1.8.2]: https://github.com/abrahamADSK/maya-mcp/compare/v1.8.1...v1.8.2
[1.8.1]: https://github.com/abrahamADSK/maya-mcp/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.6.3...v1.7.0
[1.6.3]: https://github.com/abrahamADSK/maya-mcp/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/abrahamADSK/maya-mcp/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/abrahamADSK/maya-mcp/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.3.0...v1.4.0
