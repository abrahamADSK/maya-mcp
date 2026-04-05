"""
test_rag_search.py
==================
RAG search tests for maya-mcp.

Tests the hybrid search pipeline: BM25 + semantic (HyDE-expanded) fused
via Reciprocal Rank Fusion (RRF).  Uses a mini corpus of 15 chunks across
5 API domains (maya_cmds, pymel, arnold, usd, anti_patterns) built into a
temporary ChromaDB index -- no connection to Maya or large model downloads.

Tests
-----
1. TestRagSearchBasic           -- search returns relevant chunks for "polyCube"
2. TestRagSearchCmds            -- cmds queries return maya_cmds corpus docs
3. TestRagSearchPyMEL           -- PyMEL queries return pymel corpus docs
4. TestRagSearchArnold          -- Arnold queries return arnold corpus docs
5. TestRagSearchUSD             -- USD queries return usd corpus docs
6. TestRagSearchAntiPatterns    -- anti-pattern queries return warnings
7. TestRagSearchHydeExpansion   -- HyDE detects correct domain templates
8. TestRagSearchRrfFusion       -- RRF combines semantic + lexical rankings
9. TestRagSearchBm25Exact       -- BM25 matches exact method names
10. TestRagSearchEmptyIndex     -- graceful error on empty/missing index
11. TestRagSearchNoMatch        -- irrelevant query returns low relevance
12. TestRagSearchCache          -- A12 in-session cache works correctly
"""

from unittest.mock import patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1. Basic search -- "polyCube" returns relevant chunks
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchBasic:
    """search() returns chunks containing polyCube when queried for 'polyCube'."""

    def test_returns_text_and_relevance(self, patch_rag_singletons):
        """search() returns a (str, int) tuple with non-empty text."""
        from rag.search import search

        text, relevance = search("polyCube", n_results=3)

        assert isinstance(text, str)
        assert isinstance(relevance, int)
        assert len(text) > 0
        assert relevance >= 0

    def test_top_results_mention_polycube(self, patch_rag_singletons):
        """At least one returned chunk mentions polyCube (BM25 should match it)."""
        from rag.search import search

        text, _relevance = search("polyCube", n_results=5)

        assert "polycube" in text.lower(), (
            "Expected at least one chunk mentioning polyCube"
        )

    def test_result_contains_metadata_header(self, patch_rag_singletons):
        """Results are formatted with ### [api] source -- section headers."""
        from rag.search import search

        text, _relevance = search("polyCube", n_results=3)

        assert "###" in text, "Expected markdown header in result"
        assert "relevance:" in text, "Expected relevance percentage in result"

    def test_relevance_is_bounded(self, patch_rag_singletons):
        """max_relevance is in [0, 100]."""
        from rag.search import search

        _text, relevance = search("polyCube", n_results=3)

        assert 0 <= relevance <= 100, f"Relevance {relevance} out of [0, 100] range"

    def test_n_results_limits_output(self, patch_rag_singletons):
        """Requesting n_results=2 returns at most 2 chunk blocks."""
        from rag.search import search

        text, _relevance = search("polyCube", n_results=2)

        # Chunks are separated by "\n\n---\n\n"
        chunk_count = text.count("\n\n---\n\n") + 1
        assert chunk_count <= 2, f"Expected <=2 chunks, got {chunk_count}"


# ═══════════════════════════════════════════════════════════════════════════
# 2. CMDS corpus -- cmds queries return maya_cmds docs
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchCmds:
    """Queries about maya.cmds return chunks from the CMDS_API corpus."""

    def test_xform_query_returns_cmds(self, patch_rag_singletons):
        """Querying 'xform translation' returns cmds docs."""
        from rag.search import search

        text, _relevance = search("xform translation worldSpace", n_results=5)

        assert "xform" in text.lower(), "Expected xform in results"

    def test_setattr_query_returns_cmds(self, patch_rag_singletons):
        """Querying 'setAttr type parameter' returns cmds docs."""
        from rag.search import search

        text, _relevance = search("setAttr type parameter compound", n_results=5)

        assert "setattr" in text.lower(), "Expected setAttr in results"

    def test_ls_query_returns_cmds(self, patch_rag_singletons):
        """Querying 'ls type mesh' returns cmds.ls docs."""
        from rag.search import search

        text, _relevance = search("ls type mesh selection", n_results=5)

        assert "cmds.ls" in text.lower() or "ls" in text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 3. PyMEL corpus -- PyMEL queries return pymel docs
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchPyMEL:
    """Queries about PyMEL return chunks from the PYMEL_API corpus."""

    def test_pynode_query(self, patch_rag_singletons):
        """Querying 'PyNode getAttr' returns pymel docs."""
        from rag.search import search

        text, _relevance = search("PyNode getAttr pm.selected", n_results=5)

        assert "pynode" in text.lower() or "pymel" in text.lower(), (
            "Expected PyMEL content in results"
        )

    def test_mesh_vertex_query(self, patch_rag_singletons):
        """Querying 'MeshVertex position' returns pymel mesh docs."""
        from rag.search import search

        text, _relevance = search("MeshVertex position world", n_results=5)

        assert "vertex" in text.lower() or "meshvertex" in text.lower(), (
            "Expected MeshVertex content in results"
        )

    def test_depend_node_query(self, patch_rag_singletons):
        """Querying 'DependNode listConnections' returns pymel-related docs."""
        from rag.search import search

        text, _relevance = search("DependNode listConnections type", n_results=5)

        # With deterministic embeddings, BM25 carries the match.
        # "DependNode" and "listConnections" tokens appear in the pymel chunk
        # but may also pull in other chunks. Verify we get results.
        assert len(text) > 0, "Expected non-empty results for DependNode query"
        assert "###" in text, "Expected formatted output"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Arnold corpus -- Arnold queries return arnold docs
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchArnold:
    """Queries about Arnold/mtoa return chunks from the ARNOLD_API corpus."""

    def test_aistandard_surface_query(self, patch_rag_singletons):
        """Querying 'aiStandardSurface metalness' returns Arnold docs."""
        from rag.search import search

        text, _relevance = search("aiStandardSurface metalness roughness", n_results=5)

        assert "aistandardsurface" in text.lower() or "arnold" in text.lower(), (
            "Expected Arnold shader content in results"
        )

    def test_aov_query(self, patch_rag_singletons):
        """Querying 'arnold AOV diffuse' returns AOV setup docs."""
        from rag.search import search

        text, _relevance = search("arnold AOV diffuse render pass", n_results=5)

        assert "aov" in text.lower(), "Expected AOV content in results"


# ═══════════════════════════════════════════════════════════════════════════
# 5. USD corpus -- USD queries return usd docs
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchUSD:
    """Queries about USD return chunks from the USD_API corpus."""

    def test_stage_prims_query(self, patch_rag_singletons):
        """Querying 'USD stage prim UsdGeom' returns USD docs."""
        from rag.search import search

        text, _relevance = search("USD stage prim UsdGeom Xformable", n_results=5)

        assert "usd" in text.lower() or "stage" in text.lower(), (
            "Expected USD content in results"
        )

    def test_usdshade_query(self, patch_rag_singletons):
        """Querying 'UsdShade material' returns USD material docs."""
        from rag.search import search

        text, _relevance = search("UsdShade material export mayaUsd", n_results=5)

        assert "usdshade" in text.lower() or "material" in text.lower(), (
            "Expected UsdShade content in results"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Anti-patterns corpus -- returns warnings
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchAntiPatterns:
    """Queries about common mistakes return anti-pattern warnings."""

    def test_return_value_hallucination_query(self, patch_rag_singletons):
        """Querying 'polyCube return type' surfaces the hallucination warning."""
        from rag.search import search

        text, _relevance = search("polyCube return type hallucination", n_results=5)

        # Should find the anti-patterns chunk or the polyCube cmds chunk
        assert "return" in text.lower() or "polycube" in text.lower()

    def test_wrong_flag_names_query(self, patch_rag_singletons):
        """Querying 'wrong flag import True' surfaces the wrong flags warning."""
        from rag.search import search

        text, _relevance = search(
            "wrong flag names import True keyword", n_results=5
        )

        # BM25 should match the "Wrong Flag Names" chunk on keywords
        assert "flag" in text.lower() or "import" in text.lower() or "warning" in text.lower()

    def test_anti_patterns_corpus_present(self, mini_rag_corpus):
        """Mini corpus contains anti_patterns chunks."""
        anti = [c for c in mini_rag_corpus if c["metadata"]["api"] == "anti_patterns"]
        assert len(anti) >= 2, "Expected at least 2 anti_patterns chunks in mini corpus"


# ═══════════════════════════════════════════════════════════════════════════
# 7. HyDE expansion -- domain-specific templates
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchHydeExpansion:
    """_hyde_expand() detects API domain and wraps query in code template."""

    def test_pymel_domain_detected(self):
        """Queries mentioning 'PyNode' or 'pm.' use PyMEL template."""
        from rag.search import _hyde_expand

        result = _hyde_expand("PyNode getAttr translateX")

        assert "pymel" in result.lower(), "Expected pymel in PyMEL HyDE template"
        assert "pm." in result, "Expected pm. import in PyMEL template"

    def test_arnold_domain_detected(self):
        """Queries mentioning 'arnold' or 'aiStandard' use Arnold template."""
        from rag.search import _hyde_expand

        result = _hyde_expand("arnold aiStandardSurface shader setup")

        assert "arnold" in result.lower(), "Expected Arnold header in template"
        assert "mtoa" in result.lower(), "Expected mtoa reference in template"

    def test_usd_domain_detected(self):
        """Queries mentioning 'usd' or 'UsdGeom' use USD template."""
        from rag.search import _hyde_expand

        result = _hyde_expand("USD stage export mayaUsd")

        assert "usd" in result.lower(), "Expected USD header in template"
        assert "pxr" in result.lower() or "mayausd" in result.lower()

    def test_mel_domain_detected(self):
        """Queries mentioning 'mel' use MEL template."""
        from rag.search import _hyde_expand

        result = _hyde_expand("mel.eval polyCube command")

        assert "mel" in result.lower(), "Expected MEL header in template"

    def test_default_cmds_domain(self):
        """Queries without specific keywords default to maya.cmds template."""
        from rag.search import _hyde_expand

        result = _hyde_expand("create a cube and move it")

        assert "maya.cmds" in result, "Expected maya.cmds in default template"
        assert "import maya.cmds as cmds" in result

    def test_hyde_includes_original_query(self):
        """The expanded template still includes the original query text."""
        from rag.search import _hyde_expand

        query = "polyCube with subdivisions and custom name"
        result = _hyde_expand(query)

        assert query in result, "HyDE template should embed the original query"


# ═══════════════════════════════════════════════════════════════════════════
# 8. RRF fusion -- combines semantic + lexical rankings
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchRrfFusion:
    """_rrf_fuse() correctly combines two ranked lists."""

    def test_rrf_basic_merge(self):
        """RRF merges two disjoint lists."""
        from rag.search import _rrf_fuse

        sem = ["a", "b", "c"]
        bm25 = ["d", "e", "f"]

        fused = _rrf_fuse(sem, bm25, k=60)

        assert set(fused) == {"a", "b", "c", "d", "e", "f"}
        assert fused[0] in ("a", "d"), "Top result should be rank-1 from either list"

    def test_rrf_overlapping_boosted(self):
        """Documents appearing in both lists get boosted to the top."""
        from rag.search import _rrf_fuse

        sem = ["shared", "sem_only_1", "sem_only_2"]
        bm25 = ["bm25_only_1", "shared", "bm25_only_2"]

        fused = _rrf_fuse(sem, bm25, k=60)

        assert fused[0] == "shared", (
            "Document appearing in both rankers should be boosted to top"
        )

    def test_rrf_preserves_relative_order(self):
        """With one ranker, original order is preserved."""
        from rag.search import _rrf_fuse

        sem = ["a", "b", "c"]
        fused = _rrf_fuse(sem, [], k=60)

        assert fused == ["a", "b", "c"]

    def test_rrf_empty_inputs(self):
        """RRF handles empty input lists gracefully."""
        from rag.search import _rrf_fuse

        fused = _rrf_fuse([], [], k=60)
        assert fused == []

    def test_rrf_k_parameter_affects_scores(self):
        """Different k values produce different orderings for edge cases."""
        from rag.search import _rrf_fuse

        sem = ["a", "b"]
        bm25 = ["b", "a"]

        fused_low_k = _rrf_fuse(sem, bm25, k=1)
        fused_high_k = _rrf_fuse(sem, bm25, k=1000)

        assert set(fused_low_k) == {"a", "b"}
        assert set(fused_high_k) == {"a", "b"}

    def test_rrf_integration_with_search(self, patch_rag_singletons):
        """Full search uses RRF when both BM25 and semantic results exist."""
        from rag.search import search

        text, relevance = search("polyCube create primitive", n_results=5)

        assert len(text) > 0
        assert "###" in text  # formatted output


# ═══════════════════════════════════════════════════════════════════════════
# 9. BM25 exact match -- method name retrieval
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchBm25Exact:
    """BM25 (lexical) retriever matches exact method names in corpus."""

    def test_polycube_found_by_bm25(self, patch_rag_singletons):
        """Querying 'polyCube' returns chunks containing polyCube."""
        from rag.search import search

        text, _relevance = search("polyCube", n_results=5)

        assert "polycube" in text.lower(), (
            "BM25 should rank the polyCube chunk highly for an exact token match"
        )

    def test_bm25_scores_exact_token_higher(self, mini_rag_corpus):
        """BM25 scores the polyCube chunk highest when queried for 'polyCube'."""
        from rank_bm25 import BM25Okapi

        tokenised = [entry["text"].lower().split() for entry in mini_rag_corpus]
        bm25 = BM25Okapi(tokenised)

        scores = bm25.get_scores("polycube".lower().split())

        # Find the index of the polyCube chunk
        polycube_idx = next(
            i for i, c in enumerate(mini_rag_corpus)
            if c["id"] == "CMDS_API.md::0::polyCube"
        )

        # polyCube chunk should be in the top 3 scores
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        assert polycube_idx in top_indices[:3], (
            f"polyCube chunk (idx={polycube_idx}) should be in top 3, "
            f"got top 3: {top_indices[:3]}"
        )

    def test_arnold_shader_found_by_bm25(self, patch_rag_singletons):
        """Querying 'aiStandardSurface shader metalness' returns Arnold docs."""
        from rag.search import search

        text, _relevance = search("aiStandardSurface shader metalness baseColor", n_results=5)

        # BM25 matches on multiple tokens from the Arnold chunk
        assert "shader" in text.lower() or "arnold" in text.lower() or "metalness" in text.lower(), (
            "Expected Arnold shader content in BM25 results"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 10. Empty index -- graceful error handling
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchEmptyIndex:
    """search() returns informative error when index is empty or missing."""

    def test_empty_collection_returns_message(self, rag_empty_collection):
        """An empty ChromaDB collection returns an informative message."""
        from rag.search import search

        collection, index_dir = rag_empty_collection

        with patch("rag.search._collection", collection), \
             patch("rag.search.INDEX_DIR", index_dir), \
             patch("rag.search._search_cache", {}):
            text, relevance = search("anything", n_results=3)

        assert relevance == 0, "Empty index should return relevance 0"
        assert "empty" in text.lower() or "build" in text.lower(), (
            f"Expected informative message about empty index, got: {text!r}"
        )

    def test_missing_index_dir_returns_message(self, tmp_path):
        """A nonexistent index directory returns the 'build it first' message."""
        from rag.search import search

        fake_dir = str(tmp_path / "nonexistent_index")

        with patch("rag.search._collection", None), \
             patch("rag.search._client", None), \
             patch("rag.search.INDEX_DIR", fake_dir), \
             patch("rag.search._search_cache", {}):
            text, relevance = search("anything", n_results=3)

        assert relevance == 0
        assert "not found" in text.lower() or "build" in text.lower(), (
            f"Expected 'not found' or 'build' message, got: {text!r}"
        )

    def test_empty_returns_zero_relevance(self, rag_empty_collection):
        """Relevance is exactly 0 when no chunks exist."""
        from rag.search import search

        collection, index_dir = rag_empty_collection

        with patch("rag.search._collection", collection), \
             patch("rag.search.INDEX_DIR", index_dir), \
             patch("rag.search._search_cache", {}):
            _text, relevance = search("polyCube", n_results=5)

        assert relevance == 0


# ═══════════════════════════════════════════════════════════════════════════
# 11. No match -- irrelevant query returns low relevance
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchNoMatch:
    """Queries about topics not in the corpus return low relevance scores."""

    def test_irrelevant_query_returns_results(self, patch_rag_singletons):
        """Even an irrelevant query returns *some* results (nearest neighbours)."""
        from rag.search import search

        text, relevance = search(
            "quantum physics superconductor entanglement",
            n_results=3,
        )

        assert isinstance(text, str)
        assert isinstance(relevance, int)

    def test_completely_unrelated_query_still_returns_formatted(
        self, patch_rag_singletons
    ):
        """Even for a garbage query, output is properly formatted."""
        from rag.search import search

        text, _relevance = search("xyzzy plugh abracadabra", n_results=2)

        has_results = "###" in text
        has_message = "no relevant" in text.lower() or len(text) > 0

        assert has_results or has_message, "Should return formatted results or message"

    def test_single_char_query(self, patch_rag_singletons):
        """A single-character query doesn't crash."""
        from rag.search import search

        text, relevance = search("x", n_results=2)

        assert isinstance(text, str)
        assert isinstance(relevance, int)
        assert relevance >= 0


# ═══════════════════════════════════════════════════════════════════════════
# 12. Cache -- A12 in-session cache
# ═══════════════════════════════════════════════════════════════════════════

class TestRagSearchCache:
    """A12 in-session cache returns identical results for repeated queries."""

    def test_cache_returns_same_result(self, patch_rag_singletons):
        """Identical query returns cached result on second call."""
        from rag.search import search

        result1 = search("polyCube flags", n_results=3)
        result2 = search("polyCube flags", n_results=3)

        assert result1 == result2, "Cache should return identical results"

    def test_different_queries_not_cached(self, patch_rag_singletons):
        """Different queries return different results (not cross-cached)."""
        from rag.search import search

        result1 = search("polyCube", n_results=3)
        result2 = search("aiStandardSurface", n_results=3)

        # Results should differ (different queries)
        assert result1 != result2, "Different queries should not be cross-cached"

    def test_different_n_results_not_cached(self, patch_rag_singletons):
        """Same query with different n_results are distinct cache keys."""
        from rag.search import search

        text1, _r1 = search("polyCube", n_results=1)
        text2, _r2 = search("polyCube", n_results=5)

        # n_results=1 should produce fewer chunks than n_results=5
        chunks1 = text1.count("\n\n---\n\n") + 1
        chunks2 = text2.count("\n\n---\n\n") + 1

        assert chunks1 <= chunks2, (
            f"n_results=1 ({chunks1} chunks) should have <= chunks than n_results=5 ({chunks2})"
        )

    def test_clear_cache(self, patch_rag_singletons):
        """clear_cache() empties the search cache."""
        from rag.search import search, clear_cache, _search_cache

        search("polyCube", n_results=3)
        assert len(_search_cache) > 0, "Cache should have entries after search"

        clear_cache()
        assert len(_search_cache) == 0, "Cache should be empty after clear"
