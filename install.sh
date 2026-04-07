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
#   3. Installs Python dependencies from core/requirements.txt
#   4. Builds the RAG index via core/rag/build_index.py
#   5. Registers (or updates) the MCP server entry in ~/.claude.json
#   6. Prints an installation summary
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
CORE_DIR="${REPO_ROOT}/core"
REQUIREMENTS="${CORE_DIR}/requirements.txt"
BUILD_INDEX="${CORE_DIR}/rag/build_index.py"
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
info "Step 1/5 — Checking Python version..."

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
info "Step 2/5 — Setting up virtual environment..."

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
# STEP 3 — Install dependencies from core/requirements.txt
# =============================================================================
info "Step 3/5 — Installing Python dependencies..."

if [[ ! -f "${REQUIREMENTS}" ]]; then
    error "requirements.txt not found at ${REQUIREMENTS}"
    STEPS_ERR+=("requirements.txt missing — dependencies not installed")
    # Non-fatal: continue so the rest of the script runs
else
    # Upgrade pip silently first to avoid resolver warnings
    "${VENV_PIP}" install --quiet --upgrade pip

    # Install from core/requirements.txt
    # Using --quiet to reduce noise; errors still propagate via set -e
    "${VENV_PIP}" install --quiet -r "${REQUIREMENTS}"
    success "Core dependencies installed from core/requirements.txt"
    STEPS_OK+=("Dependencies installed (core/requirements.txt)")

    # RAG extras: chromadb and sentence-transformers are required for the index.
    # They are NOT listed in core/requirements.txt (kept lean for server runtime)
    # but are needed to build and query the RAG index.
    RAG_EXTRAS="chromadb sentence-transformers rank-bm25"
    info "Installing RAG extras (${RAG_EXTRAS})..."
    if "${VENV_PIP}" install --quiet ${RAG_EXTRAS} 2>/dev/null; then
        success "RAG extras installed"
        STEPS_OK+=("RAG extras installed (chromadb, sentence-transformers, rank-bm25)")
    else
        warn "RAG extras install had warnings — search_maya_docs may be unavailable"
        STEPS_WARN+=("RAG extras install had issues — check pip output manually")
    fi
fi

# =============================================================================
# STEP 4 — Build the RAG index
# =============================================================================
info "Step 4/5 — Building RAG index..."

if [[ ! -f "${BUILD_INDEX}" ]]; then
    warn "build_index.py not found at ${BUILD_INDEX} — skipping RAG build"
    STEPS_WARN+=("RAG build skipped — build_index.py not found")
else
    # Check if index already exists and appears complete (has at least one file)
    INDEX_DIR="${CORE_DIR}/rag/index"
    CORPUS_JSON="${CORE_DIR}/rag/corpus.json"

    if [[ -d "${INDEX_DIR}" && "$(ls -A "${INDEX_DIR}" 2>/dev/null)" ]] && \
       [[ -f "${CORPUS_JSON}" ]]; then
        success "RAG index already present — skipping rebuild"
        info "  (delete ${INDEX_DIR} and ${CORPUS_JSON} to force a rebuild)"
        STEPS_OK+=("RAG index already present — skipped rebuild")
    else
        info "Running build_index.py (first run downloads embedding model ~570 MB)..."
        info "This may take several minutes on first install."
        # Run from repo root so relative imports (core.rag.*) work correctly
        if (cd "${REPO_ROOT}" && "${VENV_PYTHON}" -m core.rag.build_index); then
            success "RAG index built successfully"
            STEPS_OK+=("RAG index built (core/rag/index/)")
        else
            warn "RAG index build failed — server starts but search_maya_docs will return 'index not found'"
            warn "Re-run manually: cd ${REPO_ROOT} && .venv/bin/python -m core.rag.build_index"
            STEPS_WARN+=("RAG index build failed — run manually after install")
        fi
    fi
fi

# =============================================================================
# STEP 5 — Register MCP server in ~/.claude.json
# =============================================================================
info "Step 5/5 — Registering MCP server in ~/.claude.json..."

# Full absolute paths for the server entry
MCP_COMMAND="${VENV_DIR}/bin/python"
MCP_ARG="${CORE_DIR}/server.py"
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
        --arg arg  "${MCP_ARG}" \
        --arg cwd  "${MCP_CWD}" \
        '{command: $cmd, args: [$arg], cwd: $cwd}')

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
    "args":    ["${MCP_ARG}"],
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
    echo -e "        \"args\": [\"${MCP_ARG}\"],"
    echo -e "        \"cwd\": \"${MCP_CWD}\""
    echo -e "      }"
    echo -e "    }"
    echo -e '  }'
    echo ""
fi

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
