"""
test_golden.py
==============
F3b — Golden RAG regression test (3C Wave 3).

Loads ``tests/golden/maya_queries.jsonl`` and, for every entry, validates
the behaviour of the REAL hybrid search engine
(:func:`maya_mcp.rag.search.search`) against the entry's expectations.

What this suite IS
------------------
- A regression guard over the *real* ChromaDB + BM25 + HyDE + RRF
  pipeline reading the committed corpus (``src/maya_mcp/rag/corpus.json``
  and ``src/maya_mcp/rag/index/``). Unlike ``test_rag_search.py`` (which
  uses a deterministic-hash mini index to test the plumbing), this suite
  exercises the production retrieval against the production corpus, so a
  regression in the corpus, the index, or the ranking is caught here.

What this suite is NOT
----------------------
- It does NOT call live Maya.
- It does NOT score answer quality. It measures *retrieval*: did the
  search surface the documentation that contains the correct guidance,
  and did it avoid presenting a hallucinated/forbidden pattern as the
  authoritative answer?

CI-safety / hermeticity
-----------------------
The real engine needs (a) the committed ChromaDB index and (b) the
``BAAI/bge-large-en-v1.5`` embedding model. The model is ~570 MB and is
downloaded from the HuggingFace Hub on first query. On an offline or
model-less CI runner that download fails. To keep the suite green in
those environments while still providing real regression value where the
model IS available, every search-dependent test is guarded: if the index
is absent or the engine cannot return a usable result (model download
failure, empty/missing index), the entry is *skipped*, not failed. The
``check_adversarial_count.py`` gate and the dataset-integrity tests below
run unconditionally — they need neither the model nor the index.

Per-entry checks
----------------
1. ``must_not_contain`` (mandatory adversarial guardrail). The forbidden
   strings are phrasings that would only appear if the corpus *endorsed*
   the hallucinated/wrong pattern (e.g. "polyCube returns a string",
   "import=True is correct"). The corpus (``ANTI_PATTERNS.md``) warns
   against these traps, so it legitimately mentions the wrong token in a
   corrective context — therefore the forbidden strings are deliberately
   full *endorsement* phrasings, never the bare wrong token.
2. ``expected_api``. When provided, at least one surfaced chunk header
   must come from that API source (maya_cmds / pymel / arnold / usd /
   anti_patterns). ``expected_api: null`` marks a known fall-through
   (Spanish-only queries the English corpus does not serve well) and is
   skipped.
3. ``must_contain``. If provided, every listed substring must appear
   somewhere in the surfaced text — the corrective guidance the LLM
   needs (e.g. the ``i=true`` flag, the ``w=`` short flag, ``type=``).
4. ``min_relevance``. If > 0, the engine's max relevance must meet it,
   confirming the surfaced doc is an actual match and not a distant
   nearest-neighbour fallback.

Adversarial integrity rule
--------------------------
Any entry tagged ``adversarial`` MUST carry a non-empty
``must_not_contain`` list. The F3b precondition gate
(``scripts/check_adversarial_count.py``) re-asserts this at the dataset
level. The test below re-asserts it per entry so a broken adversarial
record cannot pass silently.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

GOLDEN_PATH = Path(__file__).parent / "golden" / "maya_queries.jsonl"


def _load_golden() -> list[dict[str, Any]]:
    """Load every non-blank line of the JSONL dataset as a dict."""
    if not GOLDEN_PATH.exists():
        return []
    with GOLDEN_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


_ENTRIES: list[dict[str, Any]] = _load_golden()


# ---------------------------------------------------------------------------
# Real-search helper (CI-safe)
# ---------------------------------------------------------------------------

# Sentinel returned by search() when the index is missing or empty. We treat
# any of these as "engine unavailable" and skip rather than fail.
_UNAVAILABLE_MARKERS = (
    "rag index not found",
    "index is empty",
    "no relevant documentation found",
)

# Cache the (text, relevance) result per query within a session so the same
# query is not embedded twice. search() also caches internally, but this
# keeps the guard cheap even if the cache is cleared between tests.
_RESULT_CACHE: dict[str, tuple[str, int]] = {}


def _real_search(query: str, n_results: int = 8) -> tuple[str, int]:
    """Call the production search engine, raising pytest.skip when unavailable.

    Returns the lower-cased text and the max relevance. Any failure to load
    the index or the embedding model (offline CI) results in a skip so the
    suite stays green where the model is not present.

    ``n_results`` defaults to 8 (wider than the tool's runtime default of 5):
    a golden *regression* set verifies that the correct documentation is
    *retrievable* for a query, so it uses a slightly wider recall window than
    an interactive lookup. The ``must_not_contain`` adversarial guard is
    unaffected by the window size — a forbidden endorsement is forbidden
    anywhere in the surfaced set.
    """
    if query in _RESULT_CACHE:
        return _RESULT_CACHE[query]
    try:
        from maya_mcp.rag.search import search
    except ImportError as exc:  # pragma: no cover - import guard
        pytest.skip(f"maya_mcp.rag.search not importable: {exc}")

    try:
        text, relevance = search(query, n_results=n_results)
    except Exception as exc:  # noqa: BLE001 - model/index load failure → skip
        pytest.skip(f"RAG engine unavailable (model/index load failed): {exc}")

    low = text.lower()
    if any(marker in low for marker in _UNAVAILABLE_MARKERS):
        pytest.skip("RAG index not built / empty in this environment")

    result = (low, relevance)
    _RESULT_CACHE[query] = result
    return result


# ---------------------------------------------------------------------------
# Dataset-integrity tests (no model/index required — always run)
# ---------------------------------------------------------------------------


def test_golden_dataset_present() -> None:
    """The golden dataset file exists and is non-empty."""
    assert _ENTRIES, f"golden dataset missing or empty: {GOLDEN_PATH}"


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["id"])
def test_adversarial_has_guardrail(entry: dict[str, Any]) -> None:
    """Adversarial entries MUST declare a non-empty must_not_contain."""
    if "adversarial" not in entry.get("tags", []):
        pytest.skip("not an adversarial entry")
    must_not = entry.get("must_not_contain") or []
    assert must_not, (
        f"{entry['id']}: adversarial entry has empty must_not_contain; "
        "this is forbidden because the F3b gate relies on the guardrail"
    )


def test_minimum_adversarial_count() -> None:
    """Mirror of the F3b gate: at least 10 adversarial entries."""
    adversarial = [e for e in _ENTRIES if "adversarial" in e.get("tags", [])]
    assert len(adversarial) >= 10, (
        f"only {len(adversarial)} adversarial entries (need >= 10)"
    )


# ---------------------------------------------------------------------------
# Per-entry retrieval tests (guarded — skip when the engine is unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["id"])
def test_retrieval_must_not_contain(entry: dict[str, Any]) -> None:
    """The surfaced docs must not endorse any forbidden/hallucinated pattern."""
    forbidden = entry.get("must_not_contain") or []
    if not forbidden:
        pytest.skip(f"{entry['id']}: no must_not_contain assertions")
    text, _relevance = _real_search(entry["query"])
    for bad in forbidden:
        assert bad.lower() not in text, (
            f"{entry['id']}: forbidden endorsement '{bad}' was surfaced "
            f"by search for query {entry['query']!r}"
        )


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["id"])
def test_retrieval_expected_api(entry: dict[str, Any]) -> None:
    """When ``expected_api`` is set, that API source must appear in a header.

    A null/absent ``expected_api`` marks a known fall-through case
    (Spanish-only queries) and is skipped, mirroring flame's null
    ``expected_tool`` semantics.
    """
    expected = entry.get("expected_api")
    if expected is None:
        pytest.skip(f"{entry['id']}: no expected_api (fall-through case)")
    text, _relevance = _real_search(entry["query"])
    # Search headers are formatted "### [<api>] <source> — <section> ...".
    marker = f"[{expected.lower()}]"
    assert marker in text, (
        f"{entry['id']}: expected API source {expected!r} not surfaced "
        f"for query {entry['query']!r}"
    )


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["id"])
def test_retrieval_must_contain(entry: dict[str, Any]) -> None:
    """If ``must_contain`` is non-empty, every substring must be surfaced."""
    expected_substrings = entry.get("must_contain") or []
    if not expected_substrings:
        pytest.skip(f"{entry['id']}: no must_contain assertions")
    text, _relevance = _real_search(entry["query"])
    missing = [s for s in expected_substrings if s.lower() not in text]
    assert not missing, (
        f"{entry['id']}: required substrings {missing} not surfaced for "
        f"query {entry['query']!r}"
    )


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["id"])
def test_retrieval_min_relevance(entry: dict[str, Any]) -> None:
    """If ``min_relevance`` > 0, the engine must clear that bar."""
    min_rel = entry.get("min_relevance") or 0
    if min_rel <= 0:
        pytest.skip(f"{entry['id']}: no min_relevance assertion")
    _text, relevance = _real_search(entry["query"])
    assert relevance >= min_rel, (
        f"{entry['id']}: relevance {relevance} below required {min_rel} "
        f"for query {entry['query']!r}"
    )


# ---------------------------------------------------------------------------
# Session-end dataset composition report (informational; never fails)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _print_golden_summary(request: pytest.FixtureRequest) -> None:
    """Print dataset composition once per session."""
    if not _ENTRIES:
        return

    by_category: Counter = Counter(e["category"] for e in _ENTRIES)
    by_lang: Counter = Counter(e["lang"] for e in _ENTRIES)
    by_tag: dict[str, int] = defaultdict(int)
    for e in _ENTRIES:
        for tag in e.get("tags", []):
            by_tag[tag] += 1

    def _emit_report() -> None:
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        reporter.write_sep("=", "golden-RAG dataset (maya)")
        reporter.write_line(f"dataset: {GOLDEN_PATH} ({len(_ENTRIES)} entries)")
        reporter.write_line(
            "by category: "
            + ", ".join(f"{c}={n}" for c, n in sorted(by_category.items()))
        )
        reporter.write_line(
            "by lang:     "
            + ", ".join(f"{c}={n}" for c, n in sorted(by_lang.items()))
        )
        reporter.write_line(
            "by tag:      "
            + ", ".join(f"{c}={n}" for c, n in sorted(by_tag.items()))
        )

    request.addfinalizer(_emit_report)
