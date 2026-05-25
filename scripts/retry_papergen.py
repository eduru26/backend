"""Re-enqueue generate_paper_job for the most recently created assessment.
Lets us iterate on paper-gen without going through the teacher UI flow.
"""
import asyncio
from sqlalchemy import select
from backend.db.base import get_session_factory
from backend.db.models import Assessment
from backend.worker.queue import enqueue


async def main():
    factory = get_session_factory()
    async with factory() as s:
        a = (await s.execute(
            select(Assessment).order_by(Assessment.created_at.desc()).limit(1)
        )).scalar_one()
    print(f"re-enqueueing for {a.id} ({a.title})")
    job_id = await enqueue("generate_paper_job", assessment_id=str(a.id))
    print(f"job_id={job_id}")


asyncio.run(main())
