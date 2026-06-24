"""maya-mcp — MCP server for controlling Autodesk Maya.

Hybrid RAG search: ChromaDB semantic + BM25 lexical, fused via RRF.
Based on the proven fpt-mcp / flame-mcp architecture, adapted for Maya APIs.
"""

from dotenv import load_dotenv

# Load secrets/config from the git-ignored repo-root ``.env`` (WORLDLABS_API_KEY,
# GPU_API_KEY, GPU_API_URL, MAYA_PORT, …) on package import, mirroring fpt-mcp so
# key handling is coherent across the ecosystem — secrets live in ``.env`` and the
# code reads them via ``os.environ``. Runs here (package __init__) so it precedes
# any submodule that reads the environment.
#
# ``override=False`` (the default) is deliberate and differs from fpt-mcp's
# ``override=True``: a value already in the process environment WINS over ``.env``.
# This protects console-injected vars (e.g. the SHOTGRID_PROJECT_ID the Maya
# console passes through, Chat 69) and shell-exported keys from being clobbered,
# while ``.env`` still supplies anything unset. fpt-mcp needs override=True so its
# ShotGrid credentials beat a possibly-stale inherited value; maya-mcp has no such
# credential-must-win case, so the safe non-clobbering default applies.
load_dotenv(override=False)
