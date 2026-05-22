from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Upload, UploadKind


class UploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_sha256(self, sha256: str) -> Upload | None:
        result = await self.session.execute(
            select(Upload).where(Upload.sha256 == sha256)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        sha256: str,
        kind: UploadKind,
        uploaded_by: uuid.UUID,
        row_count: int,
    ) -> Upload:
        upload = Upload(
            sha256=sha256,
            kind=kind,
            uploaded_by=uploaded_by,
            row_count=row_count,
        )
        self.session.add(upload)
        await self.session.flush()
        await self.session.refresh(upload)
        return upload
