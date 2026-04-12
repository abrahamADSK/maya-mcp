#!/usr/bin/env bash
# =============================================================================
# install.sh — maya-mcp installer
# =============================================================================
# Automates the full installation of maya-mcp from a clean clone.
# Safe to run multiple times (idempotent).
#
# What this script does:
#   1. Verifies Python 3.10+ is available
#   2. Creates a virtual environment in .venv/ (repo root) if not present
#   3. Installs the package in editable mode (pip install -e .)
#   4. Builds the RAG index via maya_mcp.rag.build_index
#   5. Registers (or updates) the MCP server entry in ~/.claude.json
#   6. Pre-approves MCP tools in ~/.claude/settings.json
#   7. Prints an installation summary
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# Tested on: macOS (12+), Ubuntu 22.04 / Debian 12
# Requires:  Python 3.10+, bash 4+ (macOS ships bash 3 — uses /usr/bin/env bash)
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[maya-mcp]${RESET} $*"; }
success() { echo -e "${GREEN}[maya-mcp] ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}[maya-mcp] ⚠${RESET} $*"; }
error()   { echo -e "${RED}[maya-mcp] ✗${RESET} $*" >&2; }

# ── Track results for the final summary ──────────────────────────────────────
STEPS_OK=()
STEPS_WARN=()
STEPS_ERR=()

# ── Resolve repo root (works even if script is called from another directory) ─
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
PKG_DIR="${REPO_ROOT}/src/maya_mcp"
CLAUDE_JSON="${HOME}/.claude.json"

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  maya-mcp — installation${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
info "Repo root : ${REPO_ROOT}"
info "Venv dir  : ${VENV_DIR}"
echo ""

# =============================================================================
# STEP 1 — Verify Python 3.10+
# =============================================================================
info "Step 1/6 — Checking Python version..."

# Try python3 first, fall back to python
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver_ok=$("$candidate" -c "
import sys
ok = sys.version_info >= (3, 10)
print('ok' if ok else 'no')
")
        if [[ "$ver_ok" == "ok" ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    error "Python 3.10 or newer is required but was not found."
    error "Install it via your package manager or from https://python.org"
    STEPS_ERR+=("Python 3.10+ not found — installation aborted")
    exit 1
fi

PY_VERSION=$("$PYTHON_BIN" --version 2>&1)
success "Found ${PY_VERSION} at $(command -v "$PYTHON_BIN")"
STEPS_OK+=("Python version check passed (${PY_VERSION})")

# ── Check Ollama (optional — for local/free inference) ───────────────────────
info "Checking Ollama (optional)..."
if command -v ollama &>/dev/null; then
    OLLAMA_VERSION=$(ollama --version 2>/dev/null | head -1)
    success "Ollama found: ${OLLAMA_VERSION}"
else
    warn "Ollama not found — skip if using Anthropic cloud models."
    warn "  macOS: brew install ollama && brew services start ollama"
    warn "  Linux: https://ollama.com/download/linux"
fi

# =============================================================================
# STEP 2 — Create virtual environment in .venv/ (if not already present)
# =============================================================================
info "Step 2/6 — Setting up virtual environment..."

if [[ -d "${VENV_DIR}" && -f "${VENV_DIR}/bin/python" ]]; then
    success "Virtual environment already exists at .venv/ — skipping creation"
    STEPS_OK+=("Venv already present — skipped creation")
else
    info "Creating virtual environment at ${VENV_DIR}..."
    "$PYTHON_BIN" -m venv "${VENV_DIR}"
    success "Virtual environment created"
    STEPS_OK+=("Venv created at .venv/")
fi

# Point to the venv's python/pip from here on
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"

# =============================================================================
# STEP 3 — Install package in editable mode (pip install -e .)
# =============================================================================
info "Step 3/6 — Installing maya-mcp package..."

# Upgrade pip silently first to avoid resolver warnings
"${VENV_PIP}" install --quiet --upgrade pip

# Install the package (editable mode) — pyproject.toml declares all deps
# including RAG extras (chromadb, sentence-transformers, rank-bm25)
if "${VENV_PIP}" install --quiet -e "${REPO_ROOT}"; then
    success "maya-mcp package installed in editable mode"
    STEPS_OK+=("Package installed (pip install -e .)")
else
    error "pip install -e . failed — check output above"
    STEPS_ERR+=("Package install failed — dependencies may be missing")
fi

# =============================================================================
# STEP 4 — Build the RAG index
# =============================================================================
info "Step 4/6 — Building RAG index..."

# Check if index already exists and appears complete (has at least one file)
INDEX_DIR="${PKG_DIR}/rag/index"
CORPUS_JSON="${PKG_DIR}/rag/corpus.json"

if [[ -d "${INDEX_DIR}" && "$(ls -A "${INDEX_DIR}" 2>/dev/null)" ]] && \
   [[ -f "${CORPUS_JSON}" ]]; then
    success "RAG index already present — skipping rebuild"
    info "  (delete ${INDEX_DIR} and ${CORPUS_JSON} to force a rebuild)"
    STEPS_OK+=("RAG index already present — skipped rebuild")
else
    info "Running build_index.py (first run downloads embedding model ~570 MB)..."
    info "This may take several minutes on first install."
    if (cd "${REPO_ROOT}" && "${VENV_PYTHON}" -m maya_mcp.rag.build_index); then
        success "RAG index built successfully"
        STEPS_OK+=("RAG index built (src/maya_mcp/rag/index/)")
    else
        warn "RAG index build failed — server starts but search_maya_docs will return 'index not found'"
        warn "Re-run manually: cd ${REPO_ROOT} && .venv/bin/python -m maya_mcp.rag.build_index"
        STEPS_WARN+=("RAG index build failed — run manually after install")
    fi
fi

# =============================================================================
# STEP 5 — Register MCP server in ~/.claude.json
# =============================================================================
info "Step 5/6 — Registering MCP server in ~/.claude.json..."

# Entry uses `python -m maya_mcp.server` for a proper package invocation
MCP_COMMAND="${VENV_DIR}/bin/python"
MCP_ARGS='["-m", "maya_mcp.server"]'
MCP_CWD="${REPO_ROOT}"
SERVER_NAME="maya-mcp"

# ── Helper: edit ~/.claude.json with jq (preferred) or python (fallback) ─────
register_with_jq() {
    # Read existing file or start with empty object
    local existing="{}"
    if [[ -f "${CLAUDE_JSON}" ]]; then
        existing="$(cat "${CLAUDE_JSON}")"
    fi

    # Build the new server entry and merge it in
    local new_entry
    new_entry=$(jq -n \
        --arg cmd  "${MCP_COMMAND}" \
        --argjson args "${MCP_ARGS}" \
        --arg cwd  "${MCP_CWD}" \
        '{command: $cmd, args: $args, cwd: $cwd}')

    # Merge: preserve existing keys, upsert mcpServers.<SERVER_NAME>
    echo "${existing}" | jq \
        --arg name "${SERVER_NAME}" \
        --argjson entry "${new_entry}" \
        '.mcpServers[$name] = $entry' \
        > "${CLAUDE_JSON}.tmp" && mv "${CLAUDE_JSON}.tmp" "${CLAUDE_JSON}"
}

register_with_python() {
    # Python fallback — reads/writes ~/.claude.json without jq
    "${VENV_PYTHON}" - <<PYEOF
import json, os, sys

path = os.path.expanduser("${CLAUDE_JSON}")
data = {}
if os.path.isfile(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # File exists but is invalid JSON — back it up and start fresh
        import shutil, time
        backup = path + ".bak." + str(int(time.time()))
        shutil.copy2(path, backup)
        print(f"[maya-mcp] Warning: ~/.claude.json was invalid JSON — backed up to {backup}")
        data = {}

data.setdefault("mcpServers", {})
data["mcpServers"]["${SERVER_NAME}"] = {
    "command": "${MCP_COMMAND}",
    "args":    ["-m", "maya_mcp.server"],
    "cwd":     "${MCP_CWD}",
}

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("[maya-mcp] ~/.claude.json updated successfully")
PYEOF
}

# Choose jq or python depending on availability
if command -v jq &>/dev/null; then
    info "Using jq to update ~/.claude.json..."
    if register_with_jq; then
        success "MCP server registered via jq"
        STEPS_OK+=("MCP server registered in ~/.claude.json (jq)")
    else
        warn "jq update failed — falling back to Python..."
        if register_with_python; then
            success "MCP server registered via Python fallback"
            STEPS_OK+=("MCP server registered in ~/.claude.json (python fallback)")
        else
            error "Failed to register MCP server in ~/.claude.json"
            STEPS_ERR+=("MCP server registration failed — add entry manually")
        fi
    fi
else
    info "jq not found — using Python to update ~/.claude.json..."
    if register_with_python; then
        success "MCP server registered via Python"
        STEPS_OK+=("MCP server registered in ~/.claude.json (python)")
    else
        error "Failed to register MCP server in ~/.claude.json"
        STEPS_ERR+=("MCP server registration failed — add entry manually")
    fi
fi

# =============================================================================
# STEP 6 — Pre-approve MCP tools in ~/.claude/settings.json
# =============================================================================
info "Step 6/6 — Pre-approving maya-mcp tools in ~/.claude/settings.json..."

"${VENV_PYTHON}" - <<'PYEOF'
import json, os
from pathlib import Path

TOOLS = [
    "maya_launch", "maya_ping", "maya_create_primitive", "maya_assign_material",
    "maya_transform", "maya_list_scene", "maya_delete", "maya_create_light",
    "maya_create_camera", "maya_execute_python", "maya_new_scene", "maya_save_scene",
    "maya_mesh_operation", "maya_set_keyframe", "maya_import_file",
    "maya_viewport_capture", "maya_scene_snapshot", "maya_shelf_button",
    # Vision3D actions all live behind the single `maya_vision3d` dispatch tool:
    # list_servers, select_server, health, generate_image, generate_text,
    # texture, poll, download. Pre-approving the dispatch tool covers them all.
    "maya_vision3d",
    "search_maya_docs", "learn_pattern", "session_stats",
]
PREFIX = "mcp__maya-mcp__"
new_tools = {PREFIX + t for t in TOOLS}

settings_path = Path.home() / ".claude" / "settings.json"
settings_path.parent.mkdir(parents=True, exist_ok=True)

settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        pass

settings.setdefault("permissions", {}).setdefault("allow", [])
existing = set(settings["permissions"]["allow"])
merged = sorted(existing | new_tools)
new_count = len(new_tools - existing)
settings["permissions"]["allow"] = merged

tmp = str(settings_path) + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, str(settings_path))
print(f"[maya-mcp] {new_count} new tools pre-approved ({len(merged)} total in ~/.claude/settings.json)")
PYEOF

if [[ $? -eq 0 ]]; then
    success "27 maya-mcp tools pre-approved in ~/.claude/settings.json"
    STEPS_OK+=("MCP tools pre-approved in ~/.claude/settings.json (27 tools)")
else
    warn "Tool pre-approval failed — you may see permission prompts on first use"
    STEPS_WARN+=("MCP tool pre-approval failed — run manually or approve at first prompt")
fi

# =============================================================================
# CHECK — .env file (informational, non-blocking)
# =============================================================================
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ -f "${ENV_EXAMPLE}" ]]; then
        warn ".env file not found."
        warn "  Copy .env.example to .env and configure GPU_API_URL"
        warn "  Example: cp .env.example .env"
        STEPS_WARN+=(".env not found — copy from .env.example and configure GPU_API_URL")
    else
        warn ".env not found and no .env.example to copy from — create it manually."
        STEPS_WARN+=(".env not found — create manually with GPU_API_URL")
    fi
else
    success ".env file present"
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Installation summary${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

if [[ ${#STEPS_OK[@]} -gt 0 ]]; then
    for msg in "${STEPS_OK[@]}"; do
        echo -e "  ${GREEN}✓${RESET} ${msg}"
    done
fi

if [[ ${#STEPS_WARN[@]} -gt 0 ]]; then
    echo ""
    for msg in "${STEPS_WARN[@]}"; do
        echo -e "  ${YELLOW}⚠${RESET} ${msg}"
    done
fi

if [[ ${#STEPS_ERR[@]} -gt 0 ]]; then
    echo ""
    for msg in "${STEPS_ERR[@]}"; do
        echo -e "  ${RED}✗${RESET} ${msg}"
    done
fi

echo ""

# ── Next steps hint ───────────────────────────────────────────────────────────
if [[ ${#STEPS_ERR[@]} -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}maya-mcp is ready.${RESET}"
    echo ""
    echo -e "  ${BOLD}Next steps:${RESET}"
    echo -e "  1. Copy ${CYAN}.env.example${RESET} → ${CYAN}.env${RESET} and fill in your values"
    echo -e "     ${CYAN}cp .env.example .env${RESET}"
    echo -e "  2. Add the Command Port snippet to your Maya ${CYAN}userSetup.py${RESET}"
    echo -e "     (see README.md → Installation → Step 4)"
    echo -e "  3. Restart Claude Code (or run ${CYAN}claude${RESET}) — maya-mcp will appear"
    echo -e "     in your MCP server list."
    echo ""
    echo -e "  ${BOLD}Verify the entry in ~/.claude.json:${RESET}"
    if command -v jq &>/dev/null; then
        jq ".mcpServers[\"${SERVER_NAME}\"]" "${CLAUDE_JSON}" 2>/dev/null || true
    else
        "${VENV_PYTHON}" -c "
import json, os
d = json.load(open(os.path.expanduser('${CLAUDE_JSON}')))
import pprint; pprint.pprint(d.get('mcpServers', {}).get('${SERVER_NAME}', {}))
" 2>/dev/null || true
    fi
    echo ""
else
    echo -e "${RED}${BOLD}Installation completed with errors.${RESET}"
    echo -e "Review the ✗ items above and fix them before using maya-mcp."
    echo ""
    echo -e "  ${BOLD}Manual server registration (if needed):${RESET}"
    echo -e "  Add to ${CYAN}~/.claude.json${RESET} under ${CYAN}mcpServers${RESET}:"
    echo -e '  {'
    echo -e "    \"mcpServers\": {"
    echo -e "      \"${SERVER_NAME}\": {"
    echo -e "        \"command\": \"${MCP_COMMAND}\","
    echo -e "        \"args\": [\"-m\", \"maya_mcp.server\"],"
    echo -e "        \"cwd\": \"${MCP_CWD}\""
    echo -e "      }"
    echo -e "    }"
    echo -e '  }'
    echo ""
fi

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
