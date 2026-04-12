# Changelog

All notable changes to **maya-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Earlier releases (v0.1.0 … v1.3.0) are tagged in git but were not captured
in this file. Only v1.4.0 onward is documented here; consult `git log v1.3.0`
and the `HANDOFF.md` "Sesión N" blocks for history prior to that.

## [Unreleased]

_No unreleased changes._

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

[Unreleased]: https://github.com/abrahamADSK/maya-mcp/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/abrahamADSK/maya-mcp/compare/v1.3.0...v1.4.0
