"""Durable background work — arq worker + Redis-backed job queue.

The FastAPI request path stays fast: slow work (RAG embedding/ingestion,
LMS event handlers) is enqueued onto Redis and processed by a separate
`arq` worker process. Jobs survive a backend crash because they live in
Redis until acknowledged.

Modules:
  * `queue`    — `enqueue()` helper used by API routes to submit jobs.
  * `jobs`     — the job functions themselves (run inside the worker).
  * `settings` — arq `WorkerSettings` (the worker entrypoint).
"""
