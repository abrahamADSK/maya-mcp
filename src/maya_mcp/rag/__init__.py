"""RAG (Retrieval-Augmented Generation) engine for maya-mcp.

Hybrid search: ChromaDB semantic + BM25 lexical, fused via RRF.
Based on the proven fpt-mcp / flame-mcp architecture, adapted for Maya APIs.
"""
