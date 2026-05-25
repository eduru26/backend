"""Show what's in Qdrant for the most recent assessment's tenant/subject/grade."""
import asyncio
from collections import Counter
from backend.rag.qdrant_store import get_qdrant_store
from qdrant_client.models import Filter, FieldCondition, MatchValue
import uuid

TENANT = "b21deb79-bae2-4794-9149-1d83bae8d861"

async def main():
    q = get_qdrant_store()
    client = await q._ready()
    # total points
    total = await client.count(q._collection, exact=True)
    print(f"total points in collection: {total.count}")
    # for this tenant
    n_tenant = await q.count_for_tenant(tenant_id=uuid.UUID(TENANT))
    print(f"points for tenant {TENANT}: {n_tenant}")
    # scroll a sample to see payload distribution
    flt = Filter(must=[FieldCondition(key="tenant_id", match=MatchValue(value=TENANT))])
    points, _ = await client.scroll(
        q._collection, scroll_filter=flt, limit=500, with_payload=True,
    )
    subs = Counter(p.payload.get("subject") for p in points)
    grades = Counter(p.payload.get("grade") for p in points)
    titles = Counter(p.payload.get("title") for p in points)
    print(f"subjects: {dict(subs)}")
    print(f"grades:   {dict(grades)}")
    print(f"titles:   {dict(titles)}")

asyncio.run(main())
