"""maya-mcp — MCP server for controlling Autodesk Maya.

Hybrid RAG search: ChromaDB semantic + BM25 lexical, fused via RRF.
Based on the proven fpt-mcp / flame-mcp architecture, adapted for Maya APIs.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load secrets/config from THIS repo's root ``.env`` (WORLDLABS_API_KEY,
# GPU_API_KEY, GPU_API_URL, MAYA_PORT, …) on package import, so the code can read
# them via ``os.environ``. Runs here (package __init__) so it precedes any
# submodule that reads the environment.
#
# The path is resolved ABSOLUTELY from this file's location, NOT relative to the
# cwd. The fpt-mcp Qt console spawns the maya-mcp server with the *fpt-mcp* repo
# as cwd, so a bare ``load_dotenv()`` (which searches up from the cwd) loads
# ``fpt-mcp/.env`` — which has no WORLDLABS_API_KEY — and the key reads as "not
# set" (Chat 76: the World Labs generate failed at the auth check from the
# console, while it worked from a maya-rooted shell). Anchoring on ``__file__``
# makes the maya-mcp ``.env`` load regardless of who launches the server.
#
# ``override=False`` is deliberate and differs from fpt-mcp's ``override=True``: a
# value already in the process environment WINS over ``.env``. This protects
# console-injected vars (e.g. the SHOTGRID_PROJECT_ID the Maya console passes
# through, Chat 69) and shell-exported keys, while ``.env`` still supplies
# anything unset. maya-mcp has no credential-must-win case, so the non-clobbering
# default applies.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH, override=False)
