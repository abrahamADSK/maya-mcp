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
#   7. Detects installed Maya versions and writes an idempotent guarded
#      block into each userSetup.py that opens the Command Port and
#      registers the MCP Pipeline menu on Maya startup
#   8. Prints an installation summary
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

# =============================================================================
# DOCTOR — verify install completeness without re-running the installer
# =============================================================================
# Usage: ./install.sh --doctor
#
# Sweeps the install state in 8 independent checks. Each check prints PASS,
# FAIL or WARN with a concrete remediation sentence. Exit code is 0 if all
# checks pass and 1 otherwise — safe to chain in CI or pre-session hooks.
#
# The doctor is designed so that a future Claude Code session opening this
# repo can run `./install.sh --doctor` as a Phase 0 verification step BEFORE
# attempting any smoke test against Maya. This is the lesson of Chat 41:
# spending an hour diagnosing symptoms of a broken install is a waste when a
# 2-second doctor sweep would have revealed the root cause immediately.
#
# Checks:
#   1. ~/.claude.json has mcpServers.maya-mcp with a valid cwd pointing
#      at this repo.
#   2. .env file exists at repo root and does not contain placeholder
#      values like "http://your-gpu-host:8000".
#   3. GPU_API_URL placeholder detection — warns if .env still has
#      obvious placeholder values like "your-gpu-host" or "example.com".
#   4. pyproject.toml existence — FAIL if missing (package uninstallable).
#   5. For each Maya version detected (same rule as Step 7), the
#      user scripts/userSetup.py exists and contains the sentinel block
#      with the current repo root path.
#   6. If Maya is running on MAYA_PORT, TCP probe + `about -v` returns
#      real version output (not empty bytes — the Chat 41 silent cascade
#      symptom). If Maya is not running, this check is SKIP, not FAIL.
#   7. The maya-mcp package is importable from the venv (``python -c
#      "import maya_mcp.maya_bridge"`` succeeds).
#   8. Optional Vision3D connectivity — if GPU_API_URL is set and
#      non-placeholder, HTTP probe to /health or / with 3s timeout.
# =============================================================================

run_doctor() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  maya-mcp — doctor${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    info "Repo root : ${REPO_ROOT}"

    local venv_python="${VENV_DIR}/bin/python"
    local exit_code=0

    if [[ ! -x "${venv_python}" ]]; then
        error "Venv is missing: ${venv_python}"
        error "  Run './install.sh' to create it."
        return 1
    fi

    # Resolve MAYA_PORT from .env (or default 8100), used by checks 3 and 4.
    local doctor_port="8100"
    if [[ -f "${REPO_ROOT}/.env" ]]; then
        local _env_port
        _env_port=$( (grep -E '^MAYA_PORT=' "${REPO_ROOT}/.env" || true) \
                    | tail -1 | cut -d= -f2 | tr -d ' "')
        if [[ -n "${_env_port}" ]]; then
            doctor_port="${_env_port}"
        fi
    fi

    "${venv_python}" - "${REPO_ROOT}" "${CLAUDE_JSON}" "${doctor_port}" <<'PYEOF'
"""
Doctor implementation. Each check is a single function returning a
(status, message) tuple where status is one of 'PASS', 'FAIL', 'WARN',
'SKIP'. Messages must include a remediation sentence on FAIL so a user
(or a Claude session) can act on the report without reading the source.
"""
import glob
import json
import os
import re
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(sys.argv[1])
CLAUDE_JSON = Path(sys.argv[2])
MAYA_PORT = int(sys.argv[3])

SENTINEL_START = "# --- MCP Pipeline Console auto-setup ---"
SENTINEL_END = "# --- end MCP Pipeline Console ---"

RESET = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"


def _symbol(status: str) -> str:
    return {
        "PASS": f"{GREEN}✓{RESET}",
        "FAIL": f"{RED}✗{RESET}",
        "WARN": f"{YELLOW}⚠{RESET}",
        "SKIP": f"{CYAN}·{RESET}",
    }[status]


def check_claude_json() -> tuple[str, str]:
    if not CLAUDE_JSON.is_file():
        return (
            "FAIL",
            f"{CLAUDE_JSON} does not exist. "
            f"Run ./install.sh to create the MCP server entry.",
        )
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except json.JSONDecodeError as exc:
        return ("FAIL", f"{CLAUDE_JSON} is not valid JSON ({exc}). "
                        f"Restore from a backup or run ./install.sh.")
    entry = data.get("mcpServers", {}).get("maya-mcp")
    if not entry:
        return (
            "FAIL",
            f"~/.claude.json has no mcpServers.maya-mcp entry. "
            f"Run ./install.sh to register it.",
        )
    entry_cwd = entry.get("cwd", "")
    if Path(entry_cwd) != REPO_ROOT:
        return (
            "WARN",
            f"mcpServers.maya-mcp.cwd = {entry_cwd!r} but repo root is "
            f"{str(REPO_ROOT)!r}. Another maya-mcp clone may be active; "
            f"rerun ./install.sh from THIS repo if that is wrong.",
        )
    command = entry.get("command", "")
    if "/.venv/bin/python" not in command:
        return (
            "WARN",
            f"mcpServers.maya-mcp.command = {command!r} does not point at a "
            f"venv python. Rerun ./install.sh to regenerate the entry.",
        )
    return ("PASS", f"mcpServers.maya-mcp points at {entry_cwd}")


def check_env_file() -> tuple[str, str]:
    env = REPO_ROOT / ".env"
    if not env.is_file():
        return (
            "WARN",
            f".env not found at {env}. Copy .env.example → .env and set "
            f"GPU_API_URL if you plan to use Vision3D.",
        )
    content = env.read_text(errors="replace")
    placeholder_patterns = [
        r"your[-_]gpu[-_]host",
        r"your[-_]api[-_]key",
        r"<your.*>",
    ]
    hits = []
    for pat in placeholder_patterns:
        if re.search(pat, content, re.IGNORECASE):
            hits.append(pat)
    if hits:
        return (
            "FAIL",
            f".env contains unexpanded placeholders ({', '.join(hits)}). "
            f"Edit {env} and replace with real values.",
        )
    return ("PASS", f"{env} present, no placeholder markers found")


def detect_maya_versions() -> list[str]:
    """Mirror Step 7's detection: only trust application binary evidence."""
    versions = set()
    for path in glob.glob("/Applications/Autodesk/maya*/Maya.app"):
        match = re.search(r"/maya(\d{4}(?:\.\d+)?)/", path)
        if match:
            versions.add(match.group(1))
    for path in glob.glob("/usr/autodesk/maya*-x64"):
        match = re.search(r"/maya(\d{4}(?:\.\d+)?)-x64", path)
        if match:
            versions.add(match.group(1))
    return sorted(versions)


def check_user_setup() -> tuple[str, str]:
    versions = detect_maya_versions()
    if not versions:
        return (
            "SKIP",
            "No Maya installation detected on this host — Step 7 has nothing "
            "to configure. Install Maya and rerun ./install.sh.",
        )

    missing: list[str] = []
    stale: list[str] = []
    ok: list[str] = []
    expected_root = f'r"{REPO_ROOT}"'

    for version in versions:
        scripts_dir = Path.home() / "Library/Preferences/Autodesk/maya" / version / "scripts"
        if not scripts_dir.is_dir():
            scripts_dir = Path.home() / "maya" / version / "scripts"
        us = scripts_dir / "userSetup.py"
        if not us.is_file():
            missing.append(f"Maya {version}: {us}")
            continue
        content = us.read_text(errors="replace")
        if SENTINEL_START not in content or SENTINEL_END not in content:
            missing.append(f"Maya {version}: {us} has no maya-mcp sentinel block")
            continue
        if expected_root not in content:
            stale.append(f"Maya {version}: {us} references a different repo path")
            continue
        ok.append(f"Maya {version}")

    if missing:
        return (
            "FAIL",
            "userSetup.py bootstrap missing for: " + "; ".join(missing)
            + ". Run ./install.sh to write the block.",
        )
    if stale:
        return (
            "FAIL",
            "userSetup.py block references a different clone for: "
            + "; ".join(stale)
            + ". Rerun ./install.sh from this repo to upsert.",
        )
    return ("PASS", f"userSetup.py configured for {', '.join(ok)}")


def check_command_port() -> tuple[str, str]:
    """Probe Maya's Command Port. SKIP cleanly if Maya is not running."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("localhost", MAYA_PORT))
    except ConnectionRefusedError:
        return (
            "SKIP",
            f"Maya Command Port :{MAYA_PORT} is not listening. Either Maya "
            f"is not running or userSetup.py has not bootstrapped yet — "
            f"launch Maya once and rerun the doctor.",
        )
    except Exception as exc:
        return (
            "WARN",
            f"TCP probe to localhost:{MAYA_PORT} raised {type(exc).__name__}: {exc}. "
            f"Investigate firewall / port conflict.",
        )
    try:
        s.sendall(b"about -v\n")
        s.settimeout(2.0)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
    finally:
        s.close()

    if not data:
        return (
            "FAIL",
            f"Command Port :{MAYA_PORT} accepted the connection but returned "
            f"no data. Either Maya is blocked (modal dialog, long operation), "
            f"the port is in Python sourceType instead of MEL, or another "
            f"service is holding the TCP port (e.g. Flame S+W on 7001). "
            f"Inspect Maya's Script Editor for errors and rerun ./install.sh "
            f"to rewrite userSetup.py.",
        )
    # If the response looks like a Python NameError, the port is in python
    # mode. The bridge expects mel mode.
    decoded = data.decode("utf-8", errors="replace")
    if "is not defined" in decoded or "NameError" in decoded:
        return (
            "FAIL",
            f"Command Port :{MAYA_PORT} is in sourceType='python' — the "
            f"bridge sends MEL and the port must be 'mel'. Restart Maya (the "
            f"Step 7 userSetup.py opens it in the right mode) or manually "
            f"reopen it with cmds.commandPort(name=\":{MAYA_PORT}\", "
            f"sourceType=\"mel\").",
        )
    # Strip trailing nulls / newlines for the display
    version = decoded.strip("\x00\n ")
    return ("PASS", f"Command Port :{MAYA_PORT} returned Maya version {version!r}")


def check_package_importable() -> tuple[str, str]:
    try:
        import maya_mcp.maya_bridge as _  # noqa: F401
    except Exception as exc:
        return (
            "FAIL",
            f"Cannot import maya_mcp.maya_bridge from venv: "
            f"{type(exc).__name__}: {exc}. Run ./install.sh to rebuild the venv.",
        )
    return ("PASS", "maya_mcp.maya_bridge imports cleanly from venv")


def check_placeholder_env() -> tuple[str, str]:
    """Warn if GPU_API_URL still contains obvious placeholder values."""
    env = REPO_ROOT / ".env"
    if not env.is_file():
        return (
            "SKIP",
            ".env not found — placeholder check skipped. Copy .env.example "
            "to .env if you plan to use Vision3D.",
        )
    content = env.read_text(errors="replace")
    # Extract the GPU_API_URL value (ignore commented lines)
    gpu_url = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("GPU_API_URL"):
            _, _, val = stripped.partition("=")
            gpu_url = val.strip().strip('"').strip("'")
            break
    if not gpu_url:
        return (
            "SKIP",
            "GPU_API_URL is empty or not set in .env — Vision3D features "
            "will be skipped at runtime.",
        )
    placeholder_patterns = [
        r"your[-_]gpu[-_]host",
        r"example\\.com",
        r"<[^>]+>",
        r"localhost:8000$",
        r"127\\.0\\.0\\.1:8000$",
    ]
    for pat in placeholder_patterns:
        if re.search(pat, gpu_url, re.IGNORECASE):
            return (
                "WARN",
                f"GPU_API_URL looks like a placeholder ({gpu_url!r}). "
                f"Edit .env and set GPU_API_URL to your actual Vision3D "
                f"server, or leave empty to skip Vision3D features.",
            )
    return ("PASS", f"GPU_API_URL is set to {gpu_url!r} (non-placeholder)")


def check_pyproject_toml() -> tuple[str, str]:
    """Verify pyproject.toml exists in the repo root."""
    toml_path = REPO_ROOT / "pyproject.toml"
    if not toml_path.is_file():
        return (
            "FAIL",
            f"pyproject.toml not found at {toml_path}. The package cannot "
            f"be installed without it. Restore from git or re-clone the repo.",
        )
    return ("PASS", f"pyproject.toml present at {toml_path}")


def check_vision3d_connectivity() -> tuple[str, str]:
    """If GPU_API_URL is set and non-placeholder, probe it with HTTP."""
    import urllib.request
    import urllib.error

    env = REPO_ROOT / ".env"
    if not env.is_file():
        return (
            "SKIP",
            "No .env file — Vision3D connectivity check skipped.",
        )
    content = env.read_text(errors="replace")
    gpu_url = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("GPU_API_URL"):
            _, _, val = stripped.partition("=")
            gpu_url = val.strip().strip('"').strip("'")
            break
    if not gpu_url:
        return (
            "SKIP",
            "GPU_API_URL is empty — Vision3D connectivity check skipped.",
        )
    # Skip if placeholder
    placeholder_patterns = [
        r"your[-_]gpu[-_]host",
        r"example\\.com",
        r"<[^>]+>",
    ]
    for pat in placeholder_patterns:
        if re.search(pat, gpu_url, re.IGNORECASE):
            return (
                "SKIP",
                f"GPU_API_URL is a placeholder ({gpu_url!r}) — Vision3D "
                f"connectivity check skipped.",
            )
    # Try /health first, then / as fallback
    for endpoint in ["/health", "/"]:
        url = gpu_url.rstrip("/") + endpoint
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status < 500:
                    return (
                        "PASS",
                        f"Vision3D server at {gpu_url} is reachable "
                        f"({endpoint} returned HTTP {resp.status}).",
                    )
        except urllib.error.URLError:
            continue
        except Exception:
            continue
    return (
        "WARN",
        f"Vision3D server at {gpu_url} is not reachable (tried /health "
        f"and / with 3s timeout). Check that the server is running and "
        f"the URL is correct, or leave GPU_API_URL empty to skip Vision3D.",
    )


CHECKS = [
    ("claude.json entry", check_claude_json),
    (".env file", check_env_file),
    ("Placeholder env detection", check_placeholder_env),
    ("pyproject.toml", check_pyproject_toml),
    ("userSetup.py bootstrap", check_user_setup),
    ("Command Port reachability", check_command_port),
    ("maya_mcp importable", check_package_importable),
    ("Vision3D connectivity", check_vision3d_connectivity),
]


def main() -> int:
    worst = "PASS"
    rank = {"PASS": 0, "SKIP": 1, "WARN": 2, "FAIL": 3}
    for i, (label, fn) in enumerate(CHECKS, start=1):
        try:
            status, msg = fn()
        except Exception as exc:
            status, msg = "FAIL", f"check raised {type(exc).__name__}: {exc}"
        print(f"  {_symbol(status)} [{i}/{len(CHECKS)}] {BOLD}{label}{RESET}: {msg}")
        if rank[status] > rank[worst]:
            worst = status
    print("")
    if worst == "PASS":
        print(f"{GREEN}{BOLD}All checks passed — install is ready.{RESET}")
        return 0
    if worst in ("WARN", "SKIP"):
        print(f"{YELLOW}{BOLD}Install is usable but has warnings — review above.{RESET}")
        return 0
    print(f"{RED}{BOLD}Install is incomplete — fix the FAIL items above.{RESET}")
    return 1


sys.exit(main())
PYEOF
    exit_code=$?
    echo ""
    return ${exit_code}
}

# ── Argument parsing ─────────────────────────────────────────────────────────
if [[ $# -gt 0 ]]; then
    case "$1" in
        --doctor|-d)
            run_doctor
            exit $?
            ;;
        --help|-h)
            cat <<'HELPEOF'
Usage: ./install.sh [--doctor]

Commands:
  (no args)       Run the full 7-step installer.
  --doctor, -d    Sanity-check the install state without reinstalling.
                  8 checks: claude.json entry, .env contents, GPU_API_URL
                  placeholder detection, pyproject.toml existence,
                  userSetup.py bootstrap per detected Maya version, Maya
                  Command Port reachability, maya_mcp importable from
                  venv, and optional Vision3D connectivity.
                  Exits 0 on PASS/WARN/SKIP, 1 on any FAIL.
  --help, -h      Show this help.
HELPEOF
            exit 0
            ;;
        *)
            error "Unknown argument: $1"
            error "Run './install.sh --help' for usage."
            exit 2
            ;;
    esac
fi

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
info "Step 1/7 — Checking Python version..."

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
info "Step 2/7 — Setting up virtual environment..."

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
info "Step 3/7 — Installing maya-mcp package..."

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
info "Step 4/7 — Building RAG index..."

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
info "Step 5/7 — Registering MCP server in ~/.claude.json..."

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
info "Step 6/7 — Pre-approving maya-mcp tools in ~/.claude/settings.json..."

"${VENV_PYTHON}" - <<'PYEOF'
import json, os
from pathlib import Path

#
# Canonical MCP tool list for pre-approval.
#
# maya-mcp exposes exactly 14 MCP tools (decorated with @mcp.tool in
# src/maya_mcp/server.py). 9 of them are "Tier-1" Maya operations that
# are directly decorated, 2 are dispatch tools that group multiple
# actions under a single MCP tool name, and 3 are RAG / meta tools.
#
# Dispatch tools — each contains the following actions, all covered
# by pre-approving the parent dispatch tool name:
#
#   maya_session → ping, launch, new_scene, save_scene, list_scene,
#                  scene_snapshot, delete, execute_python, shelf_button
#
#   maya_vision3d → select_server, health, generate_image, generate_text,
#                   texture, poll, download
#
# Note: earlier versions of this script listed the 9 maya_session
# actions and 6 vision3d action names as if they were individual tools.
# They never were — they are dispatch actions, not @mcp.tool-decorated
# functions. The correct surface to pre-approve is the dispatch tool
# name, which is what this list now uses.
#
# concept:install_tools_list start
TOOLS = [
    # Tier-1 Maya operations (directly decorated with @mcp.tool)
    "maya_create_primitive",
    "maya_assign_material",
    "maya_transform",
    "maya_create_light",
    "maya_create_camera",
    "maya_mesh_operation",
    "maya_set_keyframe",
    "maya_import_file",
    "maya_viewport_capture",
    # Dispatch tools (cover multiple actions each — see comment above)
    "maya_session",
    "maya_vision3d",
    # RAG & intelligence
    "search_maya_docs",
    "learn_pattern",
    "session_stats",
]
# concept:install_tools_list end
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
    success "14 maya-mcp tools pre-approved in ~/.claude/settings.json"
    STEPS_OK+=("MCP tools pre-approved in ~/.claude/settings.json (14 tools)")
else
    warn "Tool pre-approval failed — you may see permission prompts on first use"
    STEPS_WARN+=("MCP tool pre-approval failed — run manually or approve at first prompt")
fi

# =============================================================================
# STEP 7 — Configure Maya userSetup.py for every detected Maya version
# =============================================================================
# Historical gap: prior to this step, install.sh stopped after registering
# the MCP server in ~/.claude.json and told the user to manually paste a
# snippet into Maya's userSetup.py. Users who skipped that step ended up
# with a registered-but-non-functional maya-mcp whose failure mode was a
# silent empty response from maya_ping (see the bridge Chat 41 incident).
#
# This step scans for installed Maya versions on this host and writes an
# idempotent guarded block to each version's user scripts/userSetup.py.
# The block:
#   - adds this repo root to sys.path for maya-mcp / console imports
#   - opens the Command Port on MAYA_PORT (from .env, or default 8100)
#     using sourceType='mel' and the name= kwarg form (Maya 2027 silently
#     ignores the positional form when sourceType is given)
#   - installs the MCP Pipeline menu via executeDeferred
#
# Reruns are safe: the block is bounded by sentinel markers and the
# installer replaces the whole region when the markers are found.
# =============================================================================
info "Step 7/7 — Configuring Maya userSetup.py for auto-bootstrap..."

# Resolve the port the userSetup snippet should use, honoring .env override.
# set -e + pipefail: grep may legitimately return 1 (no match in .env), which
# would abort the installer; the `|| true` short-circuit keeps the pipeline
# non-fatal and we inspect `_env_port` afterwards.
USERSETUP_PORT="8100"
if [[ -f "${REPO_ROOT}/.env" ]]; then
    _env_port=$( (grep -E '^MAYA_PORT=' "${REPO_ROOT}/.env" || true) \
                | tail -1 | cut -d= -f2 | tr -d ' "')
    if [[ -n "${_env_port}" ]]; then
        USERSETUP_PORT="${_env_port}"
    fi
fi

"${VENV_PYTHON}" - "${REPO_ROOT}" "${USERSETUP_PORT}" <<'PYEOF'
"""
Scan for installed Maya versions, locate each version's user scripts dir,
and write an idempotent maya-mcp guarded block into userSetup.py.
"""
import glob
import os
import re
import sys
from pathlib import Path

REPO_ROOT = sys.argv[1]
PORT = sys.argv[2]

SENTINEL_START = "# --- MCP Pipeline Console auto-setup ---"
SENTINEL_END = "# --- end MCP Pipeline Console ---"


def detect_maya_versions() -> list[str]:
    """Return a sorted list of Maya version strings that are actually
    installed on this host.

    Only trust filesystem evidence of the *application binary*:

      - macOS:   /Applications/Autodesk/maya<version>/Maya.app
      - Linux:   /usr/autodesk/maya<version>-x64

    Preference directories under ``~/Library/Preferences/Autodesk/maya/``
    or ``~/maya/`` are deliberately NOT considered signs of an install:
    uninstalling Maya leaves the prefs tree behind, and writing a
    userSetup.py into a stale prefs dir would be misleading and churn
    the next install.sh --doctor run.
    """
    versions: set[str] = set()

    for path in glob.glob("/Applications/Autodesk/maya*/Maya.app"):
        match = re.search(r"/maya(\d{4}(?:\.\d+)?)/", path)
        if match:
            versions.add(match.group(1))

    for path in glob.glob("/usr/autodesk/maya*-x64"):
        match = re.search(r"/maya(\d{4}(?:\.\d+)?)-x64", path)
        if match:
            versions.add(match.group(1))

    return sorted(versions)


def scripts_dir_for(version: str) -> Path | None:
    """Return the per-user scripts dir for a Maya version, creating it if the
    parent exists. Returns None if no preference directory is available."""
    candidates = [
        Path.home() / "Library" / "Preferences" / "Autodesk" / "maya" / version / "scripts",
        Path.home() / "maya" / version / "scripts",
    ]
    for d in candidates:
        if d.is_dir():
            return d
        # Auto-create the scripts dir if the per-version parent exists. We do
        # NOT create the entire preference tree — that is Maya's job on first
        # launch. Only fill the gap when the parent is present.
        if d.parent.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            return d
    return None


def build_block(repo_root: str, port: str) -> str:
    """Return the guarded block to write into userSetup.py.

    Rationale for each piece:
      - sys.path insertion: lets ``from console.maya_panel import ...`` work
        for the panel install path — this is the persistence mechanism for
        the retain=True workspaceControl restore that .mod files could not
        solve on Maya 2026.
      - _mcp_open_command_port uses name= kwarg on the open call because
        Maya 2027 silently ignores cmds.commandPort(":8100", sourceType=...)
        when the first arg is positional. The query form is unaffected.
      - Both helpers are wrapped in executeDeferred so Maya's main loop is
        ready before we touch UI / networking.
      - Every exception is swallowed: a broken userSetup.py would prevent
        Maya from starting at all, so fail-open is the only responsible
        choice. The doctor subcommand handles detection instead.
    """
    lines = [
        SENTINEL_START,
        "import sys as _mcp_sys",
        "",
        f'_mcp_root = r"{repo_root}"',
        "if _mcp_root not in _mcp_sys.path:",
        "    _mcp_sys.path.insert(0, _mcp_root)",
        "",
        "import maya.utils as _mcp_utils",
        "",
        "",
        "def _mcp_open_command_port():",
        '    """Open the maya-mcp Command Port (mel sourceType, name= kwarg)."""',
        "    try:",
        "        import maya.cmds as _mc",
        f'        if not _mc.commandPort(":{port}", query=True):',
        f'            _mc.commandPort(name=":{port}", sourceType="mel")',
        "    except Exception:",
        "        pass",
        "",
        "",
        "def _mcp_menu_startup():",
        '    """Register the MCP Pipeline menu if the panel module is importable."""',
        "    try:",
        "        from console.maya_panel import install_menu",
        "        import maya.cmds as _mc",
        '        if not _mc.menu("mcpPipelineMenu", exists=True):',
        "            install_menu()",
        "    except Exception:",
        "        pass",
        "",
        "",
        "_mcp_utils.executeDeferred(_mcp_open_command_port)",
        "_mcp_utils.executeDeferred(_mcp_menu_startup)",
        SENTINEL_END,
    ]
    return "\n".join(lines) + "\n"


def upsert_block(user_setup_path: Path, block: str) -> str:
    """Write the block into user_setup_path idempotently.

    Returns one of: 'created', 'updated', 'unchanged'.
    """
    existing = ""
    if user_setup_path.is_file():
        existing = user_setup_path.read_text(encoding="utf-8", errors="replace")

    if SENTINEL_START in existing:
        start = existing.index(SENTINEL_START)
        if SENTINEL_END in existing[start:]:
            end_rel = existing[start:].index(SENTINEL_END) + len(SENTINEL_END)
            before = existing[:start].rstrip("\n")
            after = existing[start + end_rel:].lstrip("\n")
            if before and not before.endswith("\n"):
                before += "\n"
            if before:
                before += "\n"
            new_content = before + block
            if after:
                new_content += "\n" + after
            if new_content == existing:
                return "unchanged"
            user_setup_path.write_text(new_content, encoding="utf-8")
            return "updated"
        # Malformed: start without end. Append a fresh end marker after the
        # block we're about to write, preserving content before the start.
        before = existing[:start].rstrip("\n")
        if before:
            before += "\n\n"
        user_setup_path.write_text(before + block, encoding="utf-8")
        return "updated"

    # No existing block — append the block, preserving any prior content.
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing:
        existing += "\n"
    user_setup_path.write_text(existing + block, encoding="utf-8")
    return "created" if not existing else "updated"


def main() -> int:
    versions = detect_maya_versions()
    if not versions:
        print(
            "[maya-mcp] No Maya installations detected in "
            "/Applications/Autodesk/ or ~/maya/. Skipping userSetup.py step.",
            flush=True,
        )
        print("[maya-mcp] Install Maya first, then rerun ./install.sh.", flush=True)
        return 0

    block = build_block(REPO_ROOT, PORT)
    wrote_any = False
    skipped_versions: list[str] = []

    for version in versions:
        scripts_dir = scripts_dir_for(version)
        if scripts_dir is None:
            skipped_versions.append(version)
            continue
        target = scripts_dir / "userSetup.py"
        try:
            action = upsert_block(target, block)
        except OSError as exc:
            print(f"[maya-mcp] FAILED to write {target}: {exc}", flush=True)
            continue
        wrote_any = True
        symbol = {"created": "+", "updated": "~", "unchanged": "="}[action]
        print(
            f"[maya-mcp] {symbol} Maya {version}: {target} ({action})",
            flush=True,
        )

    if skipped_versions:
        print(
            "[maya-mcp] Skipped (no user preference dir yet, launch Maya "
            "once to create it, then rerun install): " + ", ".join(skipped_versions),
            flush=True,
        )

    if wrote_any:
        print(
            f"[maya-mcp] Command Port will open on :{PORT} (sourceType=mel) "
            "on the next Maya launch.",
            flush=True,
        )
    return 0


sys.exit(main())
PYEOF
_us_rc=$?

if [[ $_us_rc -eq 0 ]]; then
    success "Maya userSetup.py configured (see lines above for per-version status)"
    STEPS_OK+=("Maya userSetup.py configured (Command Port :${USERSETUP_PORT}, menu install, sys.path)")
else
    warn "userSetup.py write step exited with code ${_us_rc} — see output above"
    STEPS_WARN+=("userSetup.py configuration had non-fatal errors — inspect output")
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
    echo -e "  2. Restart Maya once (Step 7 wrote a bootstrap block into"
    echo -e "     ${CYAN}userSetup.py${RESET} for every detected version — Maya picks it"
    echo -e "     up on the next launch)."
    echo -e "  3. Restart Claude Code (or run ${CYAN}claude${RESET}) — maya-mcp will appear"
    echo -e "     in your MCP server list."
    echo ""
    echo -e "  ${BOLD}Verify the install:${RESET} run ${CYAN}./install.sh --doctor${RESET} for a"
    echo -e "  8-check sanity sweep (claude.json entry, .env contents,"
    echo -e "  userSetup.py bootstrap, Maya Command Port reachability,"
    echo -e "  package importable)."
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
