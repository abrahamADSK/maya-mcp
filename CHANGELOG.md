# Changelog

All notable changes to **maya-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Earlier releases (v0.1.0 … v1.3.0) are tagged in git but were not captured
in this file. Only v1.4.0 onward is documented here; consult `git log v1.3.0`
and the `HANDOFF.md` "Sesión N" blocks for history prior to that.

## [Unreleased]

_No unreleased changes._

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

[Unreleased]: https://github.com/abrahamADSK/maya-mcp/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.3.0...v1.4.0
