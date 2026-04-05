"""
conftest.py
===========
Shared fixtures and path setup for maya-mcp tests.

Adds ``core/`` to sys.path so that ``from safety import check_dangerous``
works the same way server.py imports it (server.py lives inside core/).

Provides:
  - Mock TCP server fixtures for bridge tests.
  - Mini RAG corpus + deterministic ChromaDB fixtures for RAG search tests.
"""

import hashlib
import sys
import json
import socket
import threading
import types as _types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
# maya-mcp/core/safety.py  ←  the module under test
# maya-mcp/tests/conftest.py  ←  this file
_CORE_DIR = Path(__file__).resolve().parent.parent / "core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


# ── Shared MCP SDK stub ───────────────────────────────────────────────────────
# server.py imports `from mcp.server.fastmcp import FastMCP, Image, Context` at
# module level.  We install a minimal stub here (before any test file is
# collected) so that `import server` succeeds without the full MCP SDK.
# The guard ensures a real installation is not clobbered when the SDK is present.

if "mcp" not in sys.modules:
    _mcp_pkg = _types.ModuleType("mcp")
    _mcp_server_mod = _types.ModuleType("mcp.server")
    _mcp_fastmcp = _types.ModuleType("mcp.server.fastmcp")

    class _StubFastMCP:
        """Minimal FastMCP stand-in: captures @mcp.tool() decorators."""
        def __init__(self, *a, **kw):
            pass

        def tool(self, **kw):
            def decorator(fn):
                return fn
            return decorator

    class _StubContext:
        """Minimal Context type-annotation stand-in."""
        pass

    class _StubImage:
        """Minimal Image stand-in."""
        pass

    _mcp_fastmcp.FastMCP = _StubFastMCP
    _mcp_fastmcp.Context = _StubContext
    _mcp_fastmcp.Image = _StubImage
    _mcp_pkg.server = _mcp_server_mod
    _mcp_server_mod.fastmcp = _mcp_fastmcp

    sys.modules["mcp"] = _mcp_pkg
    sys.modules["mcp.server"] = _mcp_server_mod
    sys.modules["mcp.server.fastmcp"] = _mcp_fastmcp


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture()
def mock_ctx():
    """Provide a mock MCP Context with async info() method.

    Used by Vision3D tool tests that receive a Context parameter.
    """
    ctx = AsyncMock()
    ctx.info = AsyncMock()
    return ctx


# ── Mock TCP Server ──────────────────────────────────────────────────────────

class MockMayaTCPServer:
    """
    Lightweight TCP server that mimics Maya's Command Port.

    Accepts connections, records received commands, and replies
    with a configurable response.  Supports the dual-connection
    pattern used by MayaBridge.send_python().
    """

    def __init__(self, host: str = "localhost", port: int = 0):
        self.host = host
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]  # OS-assigned free port

        self.received_commands: list[str] = []
        self.responses: list[str] = []       # FIFO queue of replies
        self.default_response: str = "OK"
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> "MockMayaTCPServer":
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        self._srv.settimeout(0.5)
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                data = b""
                conn.settimeout(1.0)
                while True:
                    try:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        # Maya protocol: newline-terminated command
                        if b"\n" in data:
                            break
                    except socket.timeout:
                        break

                cmd = data.decode("utf-8").strip()
                self.received_commands.append(cmd)

                # Pick response from queue, or use default
                reply = self.responses.pop(0) if self.responses else self.default_response
                conn.sendall(reply.encode("utf-8"))
            except Exception:
                pass
            finally:
                conn.close()


@pytest.fixture()
def mock_maya_server():
    """Yields a running MockMayaTCPServer; stops it after the test."""
    server = MockMayaTCPServer().start()
    yield server
    server.stop()


@pytest.fixture()
def bridge_to_mock(mock_maya_server):
    """Returns a MayaBridge pointed at the mock server."""
    from maya_bridge import MayaBridge
    return MayaBridge(
        host=mock_maya_server.host,
        port=mock_maya_server.port,
        timeout=3.0,
    )


# ── RAG search fixtures ────────────────────────────────────────────────────
# Mini corpus: 15 chunks covering all 5 API domains (maya_cmds, pymel,
# arnold, usd, anti_patterns).  Small enough for fast tests, rich enough to
# exercise HyDE domain detection, BM25 exact matching, and RRF fusion.

MINI_RAG_CORPUS = [
    # ── maya_cmds domain (4 chunks) ─────────────────────────────────────
    {
        "id": "CMDS_API.md::0::polyCube",
        "text": (
            "## polyCube\n\n"
            "- `cmds.polyCube(w=1, h=1, d=1, sx=1, sy=1, sz=1, name='pCube1')`\n\n"
            "Create a polygon cube primitive.\n\n"
            "**Returns:** `[str, str]` — `[transform_node, shape_node]`\n\n"
            "**Common flags:**\n"
            "- `w` / `width` — width of cube\n"
            "- `h` / `height` — height of cube\n"
            "- `d` / `depth` — depth of cube\n"
            "- `sx` / `subdivisionsX` — subdivisions in X\n\n"
            "**Example:**\n"
            "```python\n"
            "import maya.cmds as cmds\n"
            "result = cmds.polyCube(w=2, h=2, d=2, name='myCube')\n"
            "# result = ['myCube', 'polyCube1']\n"
            "```"
        ),
        "metadata": {"source": "CMDS_API.md", "section": "polyCube", "api": "maya_cmds"},
    },
    {
        "id": "CMDS_API.md::1::xform",
        "text": (
            "## xform\n\n"
            "- `cmds.xform(obj, t=[0,0,0], ro=[0,0,0], s=[1,1,1], ws=True)`\n\n"
            "Query or set transformation values for objects.\n\n"
            "**Flags:**\n"
            "- `t` / `translation` — translation vector [x, y, z]\n"
            "- `ro` / `rotation` — rotation vector [x, y, z]\n"
            "- `s` / `scale` — scale vector [x, y, z]\n"
            "- `ws` / `worldSpace` — use world space coordinates\n"
            "- `q` / `query` — query mode\n\n"
            "```python\n"
            "cmds.xform('pCube1', t=[5, 0, 0], ws=True)\n"
            "pos = cmds.xform('pCube1', q=True, t=True, ws=True)\n"
            "```"
        ),
        "metadata": {"source": "CMDS_API.md", "section": "xform", "api": "maya_cmds"},
    },
    {
        "id": "CMDS_API.md::2::ls",
        "text": (
            "## ls\n\n"
            "- `cmds.ls(*args, type=None, sl=True, dag=False, long=False)`\n\n"
            "List objects in the scene matching optional filters.\n\n"
            "**Flags:**\n"
            "- `type` — filter by node type (e.g. 'mesh', 'transform')\n"
            "- `sl` / `selection` — list selected objects\n"
            "- `dag` — list DAG nodes only\n"
            "- `long` — return full DAG path names\n\n"
            "```python\n"
            "all_meshes = cmds.ls(type='mesh')\n"
            "selected = cmds.ls(sl=True)\n"
            "```"
        ),
        "metadata": {"source": "CMDS_API.md", "section": "ls", "api": "maya_cmds"},
    },
    {
        "id": "CMDS_API.md::3::setAttr",
        "text": (
            "## setAttr\n\n"
            "- `cmds.setAttr(attr, value, type=None)`\n\n"
            "Set the value of a node attribute.\n\n"
            "**Important:** For compound types (string, double3, etc.) "
            "you MUST specify the type= parameter.\n\n"
            "```python\n"
            "cmds.setAttr('pCube1.translateX', 5.0)\n"
            "cmds.setAttr('lambert1.color', 1, 0, 0, type='double3')\n"
            "cmds.setAttr('myNode.notes', 'hello', type='string')\n"
            "```"
        ),
        "metadata": {"source": "CMDS_API.md", "section": "setAttr", "api": "maya_cmds"},
    },
    # ── pymel domain (3 chunks) ─────────────────────────────────────────
    {
        "id": "PYMEL_API.md::0::PyNode basics",
        "text": (
            "## PyNode Basics\n\n"
            "PyMEL wraps Maya objects in Python classes. Use `pm.PyNode()` "
            "to get an object-oriented handle.\n\n"
            "```python\n"
            "import pymel.core as pm\n"
            "node = pm.PyNode('pCube1')\n"
            "node.translateX.set(5.0)\n"
            "pos = node.getTranslation(space='world')\n"
            "```\n\n"
            "- `pm.selected()` — list of selected PyNodes\n"
            "- `pm.ls(type='mesh')` — list meshes as MeshVertex/MeshFace objects\n"
            "- `node.getAttr('tx')` — get attribute value\n"
            "- `node.connections()` — list connected nodes"
        ),
        "metadata": {"source": "PYMEL_API.md", "section": "PyNode basics", "api": "pymel"},
    },
    {
        "id": "PYMEL_API.md::1::MeshVertex operations",
        "text": (
            "## MeshVertex Operations\n\n"
            "PyMEL provides component-level access to mesh vertices.\n\n"
            "```python\n"
            "import pymel.core as pm\n"
            "mesh = pm.PyNode('pCubeShape1')\n"
            "for vtx in mesh.vtx:\n"
            "    pos = vtx.getPosition(space='world')\n"
            "    vtx.setPosition(pos + pm.dt.Vector(0, 1, 0))\n"
            "```\n\n"
            "- `mesh.vtx[0]` — access vertex by index\n"
            "- `mesh.f[0:4]` — face range access\n"
            "- `mesh.numVertices()` — vertex count"
        ),
        "metadata": {"source": "PYMEL_API.md", "section": "MeshVertex operations", "api": "pymel"},
    },
    {
        "id": "PYMEL_API.md::2::DependNode",
        "text": (
            "## DependNode\n\n"
            "Base class for all dependency graph nodes in PyMEL.\n\n"
            "```python\n"
            "import pymel.core as pm\n"
            "node = pm.PyNode('lambert1')\n"
            "# DependNode methods:\n"
            "node.listAttr()        # all attributes\n"
            "node.listConnections() # connected nodes\n"
            "node.type()            # node type string\n"
            "node.rename('myMat')   # rename\n"
            "```"
        ),
        "metadata": {"source": "PYMEL_API.md", "section": "DependNode", "api": "pymel"},
    },
    # ── arnold domain (2 chunks) ────────────────────────────────────────
    {
        "id": "ARNOLD_API.md::0::aiStandardSurface",
        "text": (
            "## aiStandardSurface\n\n"
            "The primary Arnold shader in Maya (mtoa).\n\n"
            "```python\n"
            "import maya.cmds as cmds\n"
            "shader = cmds.shadingNode('aiStandardSurface', asShader=True)\n"
            "cmds.setAttr(f'{shader}.baseColor', 0.8, 0.2, 0.2, type='double3')\n"
            "cmds.setAttr(f'{shader}.metalness', 1.0)\n"
            "cmds.setAttr(f'{shader}.specularRoughness', 0.3)\n"
            "```\n\n"
            "**Common attributes:** baseColor, metalness, specular, "
            "specularRoughness, transmission, emission, coat."
        ),
        "metadata": {"source": "ARNOLD_API.md", "section": "aiStandardSurface", "api": "arnold"},
    },
    {
        "id": "ARNOLD_API.md::1::AOV Setup",
        "text": (
            "## AOV Setup\n\n"
            "Arnold AOVs (Arbitrary Output Variables) for render passes.\n\n"
            "```python\n"
            "import maya.cmds as cmds\n"
            "# Enable AOV system\n"
            "cmds.setAttr('defaultArnoldRenderOptions.aovMode', 1)\n"
            "# Create a diffuse AOV\n"
            "aov = cmds.createNode('aiAOV', name='aiAOV_diffuse')\n"
            "cmds.setAttr(f'{aov}.name', 'diffuse', type='string')\n"
            "```\n\n"
            "Built-in AOVs: diffuse, specular, transmission, sss, emission, "
            "N, P, Z, crypto_asset, crypto_material."
        ),
        "metadata": {"source": "ARNOLD_API.md", "section": "AOV Setup", "api": "arnold"},
    },
    # ── usd domain (2 chunks) ──────────────────────────────────────────
    {
        "id": "USD_API.md::0::Stage and Prims",
        "text": (
            "## USD Stage and Prims\n\n"
            "USD (Universal Scene Description) in Maya via mayaUsd plugin.\n\n"
            "```python\n"
            "from pxr import Usd, UsdGeom, Sdf\n"
            "stage = Usd.Stage.Open('/path/scene.usda')\n"
            "prim = stage.GetPrimAtPath('/World/Cube')\n"
            "xform = UsdGeom.Xformable(prim)\n"
            "```\n\n"
            "**Maya-USD commands:**\n"
            "- `cmds.mayaUSDExport(file='/out.usd', selection=True)`\n"
            "- `cmds.mayaUSDImport(file='/in.usd')`\n"
            "- `cmds.mayaUsdProxyShape(create=True)`"
        ),
        "metadata": {"source": "USD_API.md", "section": "Stage and Prims", "api": "usd"},
    },
    {
        "id": "USD_API.md::1::UsdShade materials",
        "text": (
            "## UsdShade Materials\n\n"
            "Material assignment in USD stages using UsdShade.\n\n"
            "```python\n"
            "from pxr import UsdShade, Sdf\n"
            "material = UsdShade.Material.Define(stage, '/World/Materials/myMat')\n"
            "shader = UsdShade.Shader.Define(stage, '/World/Materials/myMat/PBR')\n"
            "shader.CreateIdAttr('UsdPreviewSurface')\n"
            "material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')\n"
            "```\n\n"
            "Export materials: `cmds.mayaUSDExport(shadingMode='useRegistry')`"
        ),
        "metadata": {"source": "USD_API.md", "section": "UsdShade materials", "api": "usd"},
    },
    # ── anti_patterns domain (2 chunks) ─────────────────────────────────
    {
        "id": "ANTI_PATTERNS.md::0::Return value hallucinations",
        "text": (
            "## Return Value Hallucinations\n\n"
            "**WARNING:** LLMs frequently hallucinate return types.\n\n"
            "- `cmds.polyCube()` returns `[str, str]` (transform, shape), NOT a string\n"
            "- `cmds.ls()` returns a list, even for single matches\n"
            "- `cmds.file(q=True, sceneName=True)` returns a string, not a list\n"
            "- `cmds.getAttr('node.worldMatrix')` returns a flat list of 16 floats\n\n"
            "Always verify return types against documentation before using results."
        ),
        "metadata": {"source": "ANTI_PATTERNS.md", "section": "Return value hallucinations", "api": "anti_patterns"},
    },
    {
        "id": "ANTI_PATTERNS.md::1::Wrong flag names",
        "text": (
            "## Wrong Flag Names\n\n"
            "**WARNING:** Common flag name hallucinations:\n\n"
            "- `cmds.file(import=True)` — WRONG! `import` is a Python keyword. "
            "Use `i=True` or `importFile=True`.\n"
            "- `cmds.polyCube(width=1)` — WRONG! Use short form `w=1`.\n"
            "- `cmds.setAttr('node.color', [1,0,0])` — WRONG for compound types! "
            "Must pass `type='double3'`.\n"
            "- `cmds.xform(worldSpace=True)` — WRONG! Use `ws=True`.\n\n"
            "Always check the exact flag names in documentation."
        ),
        "metadata": {"source": "ANTI_PATTERNS.md", "section": "Wrong flag names", "api": "anti_patterns"},
    },
    # ── Filler chunk (for no-match tests) ───────────────────────────────
    {
        "id": "CMDS_API.md::99::Changelog",
        "text": (
            "## Changelog\n\n"
            "- v2024.2: Added USD export improvements\n"
            "- v2024.1: New polyCut tool\n"
            "- v2023.3: Improved viewport 2.0 performance"
        ),
        "metadata": {"source": "CMDS_API.md", "section": "Changelog", "api": "maya_cmds"},
    },
]


def _make_deterministic_embedding_fn():
    """Build a ChromaDB-compatible deterministic embedding function.

    Generates 64-dimensional vectors from a SHA-256 hash of the input text.
    No model download required — fast and reproducible.  Semantically similar
    texts will NOT produce similar vectors (this is a hash, not a learned
    embedding), but that's fine for testing the search *plumbing*: indexing,
    BM25, RRF fusion, formatting, and error handling.
    """
    import chromadb

    class _DetEF(chromadb.EmbeddingFunction):
        def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
            vectors = []
            for text in input:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                vec = [(b / 127.5) - 1.0 for b in digest]
                vec = (vec * 2)[:64]
                vectors.append(vec)
            return vectors

        @staticmethod
        def name() -> str:
            return "deterministic_test"

        def build_from_config(self, config):
            return _DetEF()

        def get_config(self):
            return {}

    return _DetEF()


@pytest.fixture
def mini_rag_corpus():
    """Return a copy of the mini RAG corpus (15 chunks, 5 API domains)."""
    import copy
    return copy.deepcopy(MINI_RAG_CORPUS)


@pytest.fixture
def rag_chroma_collection(tmp_path, mini_rag_corpus):
    """Build a temporary ChromaDB collection from the mini corpus.

    Returns (collection, index_dir) where index_dir is a str path to the
    temporary ChromaDB persistent directory.
    """
    import chromadb

    index_dir = str(tmp_path / "rag_index")
    client = chromadb.PersistentClient(path=index_dir)

    embedding_fn = _make_deterministic_embedding_fn()
    collection = client.create_collection(
        name="maya_docs",
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    collection.add(
        ids=[c["id"] for c in mini_rag_corpus],
        documents=[c["text"] for c in mini_rag_corpus],
        metadatas=[c["metadata"] for c in mini_rag_corpus],
    )

    return collection, index_dir


@pytest.fixture
def rag_corpus_json(tmp_path, mini_rag_corpus):
    """Write the mini corpus as corpus.json for BM25 and return the path."""
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps(mini_rag_corpus, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(corpus_path)


@pytest.fixture
def rag_empty_collection(tmp_path):
    """Build an empty ChromaDB collection (0 chunks) for edge-case tests.

    Returns (collection, index_dir).
    """
    import chromadb

    index_dir = str(tmp_path / "empty_index")
    client = chromadb.PersistentClient(path=index_dir)

    embedding_fn = _make_deterministic_embedding_fn()
    collection = client.create_collection(
        name="maya_docs",
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    return collection, index_dir


@pytest.fixture
def patch_rag_singletons(rag_chroma_collection, rag_corpus_json):
    """Patch search.py module-level singletons to use the test index.

    Replaces:
      - _collection -> test ChromaDB collection
      - _bm25 / _bm25_docs -> BM25 built from mini corpus
      - INDEX_DIR -> test index directory
      - CORPUS_PATH -> test corpus.json
      - _search_cache -> fresh empty dict

    Yields (collection, bm25, bm25_docs) for assertions.
    """
    from rank_bm25 import BM25Okapi
    from rag.search import search as _ensure_imported  # noqa: F401

    collection, index_dir = rag_chroma_collection

    # Build BM25 from the corpus file
    with open(rag_corpus_json, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    tokenised = [entry["text"].lower().split() for entry in corpus]
    bm25 = BM25Okapi(tokenised)

    with patch("rag.search._collection", collection), \
         patch("rag.search._bm25", bm25), \
         patch("rag.search._bm25_docs", corpus), \
         patch("rag.search.INDEX_DIR", index_dir), \
         patch("rag.search.CORPUS_PATH", rag_corpus_json), \
         patch("rag.search._search_cache", {}):
        yield collection, bm25, corpus
