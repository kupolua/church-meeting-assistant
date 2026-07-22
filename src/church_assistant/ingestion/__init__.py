"""
Ingestion worker package (MVP-C).

Background consumer that drives the long audio → protocol pipeline as async
jobs (mirrors the query worker, but for meeting ingestion with a human-in-the-
loop speakers.json review pause).

Modules:
    config     — INGESTION_* environment settings
    paths      — resolve per-meeting artifact paths (audio, rttm, speakers, …)
    stages     — async subprocess wrappers around the pipeline CLI modules
    processor  — run one dequeued job (dispatch by status)
    main       — consumer loop (health-gated, graceful shutdown)
"""
