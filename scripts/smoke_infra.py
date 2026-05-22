"""Smoke test for the infra patterns: caching, message queue, breakers.

Run inside the backend container:
    docker compose exec -T backend python //app/scripts/smoke_infra.py
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select


async def test_caching() -> None:
    print("\n=== CACHING (embedding cache) ===")
    from backend.rag.embeddings import get_embedder

    embedder = get_embedder()
    text = "The school leave policy permits five days per term."

    t0 = time.perf_counter()
    v1 = await embedder.embed(text)
    miss_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    v2 = await embedder.embed(text)
    hit_ms = (time.perf_counter() - t1) * 1000

    print(f"  1st call (miss): {miss_ms:.1f} ms")
    print(f"  2nd call (hit):  {hit_ms:.1f} ms  ({miss_ms / max(hit_ms, 0.01):.0f}x faster)")
    print(f"  vectors identical: {v1 == v2}  dim={len(v1)}")
    assert v1 == v2 and hit_ms < miss_ms, "cache did not speed up the repeat call"
    print("  PASS")


async def test_queue() -> None:
    print("\n=== MESSAGE QUEUE (arq enqueue → worker → result) ===")
    from backend.db.base import get_session_factory
    from backend.db.models import Tenant
    from backend.worker.queue import enqueue, job_status

    factory = get_session_factory()
    async with factory() as session:
        tenant = (await session.execute(select(Tenant).limit(1))).scalar_one()

    job_id = await enqueue("reindex_job", tenant_id=str(tenant.id))
    print(f"  enqueued reindex_job: job_id={job_id}")
    assert job_id, "enqueue returned no job id — queue unreachable"

    # Poll up to 60s for the worker to pick it up and finish.
    for _ in range(30):
        await asyncio.sleep(2)
        info = await job_status(job_id)
        if info.get("status") == "complete":
            print(f"  worker completed job: result={info.get('result')}")
            print("  PASS")
            return
    print(f"  last status: {await job_status(job_id)}")
    raise AssertionError("job did not complete within 60s")


async def test_breakers() -> None:
    print("\n=== CIRCUIT BREAKERS (registry after calls) ===")
    from backend.core.circuit_breaker import all_breakers

    breakers = all_breakers()
    for b in breakers:
        print(f"  {b['name']}: state={b['state']} failures={b['consecutive_failures']}")
    names = {b["name"] for b in breakers}
    # ollama_embed should now exist because test_caching ran an embed.
    assert "ollama_embed" in names, "ollama_embed breaker not registered"
    print("  PASS — breakers registered and reporting state")


async def main() -> None:
    await test_caching()
    await test_breakers()
    await test_queue()
    print("\nALL INFRA SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
