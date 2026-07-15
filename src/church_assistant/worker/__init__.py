"""
Background worker package (MVP-A.4).

Consumes pending queries from the queue, runs the RAG pipeline (Voyage embed →
Qdrant → rerank → Gemma), stores results, and delivers answers back to Telegram.

Also takes periodic health snapshots of Ollama/Qdrant so the worker can pause
gracefully when a dependency is down (instead of failing every query).

Entry point:
    uv run python -m church_assistant.worker.main
"""
