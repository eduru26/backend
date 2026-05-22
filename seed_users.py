"""Create initial superadmin + default-school users.

Run inside the container:
    docker compose cp seed_users.py backend:/app/seed_users.py
    docker compose exec backend python seed_users.py
"""
import asyncio
import uuid

from backend.core.security import hash_password
from backend.db.base import get_session_factory
from backend.db.models import Classroom, Tenant, TenantPlan, User, UserRole
from backend.db.repositories.users import normalize_phone

DEFAULT_TENANT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")


async def seed() -> None:
    from sqlalchemy import select

    async with get_session_factory()() as session:

        # ── 1. Ensure default tenant exists ──────────────────
        tenant = await session.scalar(
            select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)
        )
        if not tenant:
            tenant = Tenant(
                id=DEFAULT_TENANT_ID,
                name="Demo School",
                slug="demo-school",
                plan=TenantPlan.starter,
                is_active=True,
            )
            session.add(tenant)
            await session.flush()
            print("Created tenant: Demo School")

            # Provision Grade 1-10 classrooms for the new tenant
            for grade in range(1, 11):
                session.add(Classroom(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    name=f"Grade {grade} - Section A",
                    grade=grade,
                    section="A",
                    capacity=35,
                    teacher_id=None,
                ))
            await session.flush()
            print("Provisioned Grade 1–10 classrooms (Section A, 35 seats)")
        else:
            print(f"Tenant already exists: {tenant.name}")

        # ── 2. Seed users ─────────────────────────────────────
        SEED_USERS = [
            dict(
                tenant_id=None,
                email="admin@system.com",
                phone=None,
                password="admin123",
                role=UserRole.superadmin,
                full_name="Super Admin",
            ),
            dict(
                tenant_id=DEFAULT_TENANT_ID,
                email="schooladmin@school.com",
                phone=None,
                password="school123",
                role=UserRole.school_admin,
                full_name="School Admin",
            ),
            dict(
                tenant_id=DEFAULT_TENANT_ID,
                email="teacher@school.com",
                phone=None,
                password="teacher123",
                role=UserRole.teacher,
                full_name="Mr. Smith",
            ),
            # Parent uses phone — no email
            dict(
                tenant_id=DEFAULT_TENANT_ID,
                email=None,
                phone="+919000000001",
                password="parent123",
                role=UserRole.parent,
                full_name="Mrs. Jones",
            ),
        ]

        created, skipped = [], []

        for spec in SEED_USERS:
            # Check by email (non-parents) or phone (parents)
            existing = None
            if spec["email"]:
                existing = await session.scalar(
                    select(User).where(User.email == spec["email"])
                )
            elif spec["phone"]:
                existing = await session.scalar(
                    select(User).where(User.phone == spec["phone"])
                )

            identifier = spec["email"] or spec["phone"] or "unknown"
            if existing:
                skipped.append(identifier)
                continue

            user = User(
                id=uuid.uuid4(),
                tenant_id=spec["tenant_id"],
                email=spec["email"],
                phone=spec["phone"],
                phone_digits=normalize_phone(spec["phone"]),
                password_hash=hash_password(spec["password"]),
                role=spec["role"],
                full_name=spec["full_name"],
            )
            session.add(user)
            created.append((user, identifier))

        await session.commit()

        if created:
            print("\nCreated:")
            for u, identifier in created:
                school = f"tenant: {u.tenant_id}" if u.tenant_id else "no school — superadmin"
                login = f"phone={identifier}" if u.phone else f"email={identifier}"
                print(f"  {u.role.value:12s}  {login}  ({school})")
        if skipped:
            print("\nSkipped (already exist):", ", ".join(skipped))

        print("\n── Login credentials ──────────────────────────")
        print("  Super Admin:   admin@system.com    / admin123")
        print("  School Admin:  schooladmin@school.com / school123")
        print("  Teacher:       teacher@school.com  / teacher123")
        print("  Parent:        +919000000001       / parent123  (login with phone)")
        print("\n── URLs ───────────────────────────────────────")
        print("  Super Admin panel → http://localhost:8000/admin")
        print("  School Admin      → http://localhost:8000/school-admin")
        print("  Teacher portal    → http://localhost:8000/teacher")
        print("  Chat (parent/student) → http://localhost:8000")


asyncio.run(seed())
