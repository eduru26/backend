"""Create the initial superadmin user.

Run inside the container:
    docker compose cp seed_users.py backend:/app/seed_users.py
    docker compose exec backend python seed_users.py
"""
import asyncio
import uuid

from backend.core.security import hash_password
from backend.db.base import get_session_factory
from backend.db.models import User, UserRole


async def seed() -> None:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        existing = await session.scalar(
            select(User).where(User.email == "admin@system.com")
        )
        if existing:
            print("Superadmin already exists — skipping.")
            return

        user = User(
            id=uuid.uuid4(),
            tenant_id=None,
            email="admin@system.com",
            phone=None,
            phone_digits=None,
            password_hash=hash_password("admin123"),
            role=UserRole.superadmin,
            full_name="Super Admin",
        )
        session.add(user)
        await session.commit()

        print("Created superadmin: admin@system.com / admin123")


asyncio.run(seed())
