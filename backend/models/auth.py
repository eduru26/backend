from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from backend.db.models import UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_min: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: str | None       # nullable — parents may log in by phone only
    phone: str | None
    role: UserRole
    full_name: str | None
    specialization: str | None
    is_active: bool
