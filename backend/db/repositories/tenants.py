from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Student, Tenant, TenantPlan, User


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[Tenant]:
        result = await self._session.execute(select(Tenant).order_by(Tenant.name))
        return list(result.scalars().all())

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        result = await self._session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self._session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        return result.scalar_one_or_none()

    async def create(self, name: str, slug: str, plan: TenantPlan = TenantPlan.starter) -> Tenant:
        tenant = Tenant(id=uuid.uuid4(), name=name, slug=slug, plan=plan)
        self._session.add(tenant)
        await self._session.flush()
        await self._session.refresh(tenant)
        return tenant

    async def update(self, tenant_id: uuid.UUID, **kwargs: object) -> Tenant | None:
        tenant = await self.get_by_id(tenant_id)
        if not tenant:
            return None
        for key, value in kwargs.items():
            setattr(tenant, key, value)
        await self._session.flush()
        return tenant

    async def stats(self, tenant_id: uuid.UUID) -> dict[str, int]:
        student_count = await self._session.scalar(
            select(func.count()).where(Student.tenant_id == tenant_id)
        ) or 0
        user_count = await self._session.scalar(
            select(func.count()).where(User.tenant_id == tenant_id)
        ) or 0
        return {"students": student_count, "users": user_count}
