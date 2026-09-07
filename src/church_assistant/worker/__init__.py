"""
Background worker package (MVP-A.4).

Consumes pending queries from the queue, runs the RAG pipeline (bge-m3 embed →
Qdrant → rerank → Gemma) and stores results.

Also takes periodic health snapshots of Ollama/Qdrant so the worker can pause
gracefully when a dependency is down (instead of failing every query).

Entry point:
    uv run python -m church_assistant.worker.main
"""
