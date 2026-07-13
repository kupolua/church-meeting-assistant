"""
Shared services — reusable logic for web + bot + worker processes.

Submodules:
    - rag: async RAG pipeline (embed → search → rerank → synthesize)
    - meetings_index: read-only access to data/meetings/ folder
    - health: Ollama / Qdrant / PostgreSQL status checks
    - logger: bound-to-process convenience logger
"""
