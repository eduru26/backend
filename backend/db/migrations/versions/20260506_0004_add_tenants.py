"""add multi-tenant support

Revision ID: 0004_add_tenants
Revises: 0003_add_chat_tables
Create Date: 2026-05-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "0004_add_tenants"
down_revision: str | None = "0003_add_chat_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed UUID so the default tenant is stable across environments
_DEFAULT_TENANT_ID = "10000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ── 1. New enums ────────────────────────────────────────
    op.execute(sa.text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'superadmin'"))

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE tenant_plan AS ENUM ('starter', 'pro', 'enterprise');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # ── 2. tenants table ────────────────────────────────────
    if "tenants" not in existing_tables:
        op.create_table(
            "tenants",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
            sa.Column(
                "plan",
                PgEnum(name="tenant_plan", create_type=False),
                nullable=False,
                server_default="starter",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    # ── 3. Seed default tenant ──────────────────────────────
    op.execute(sa.text(
        f"INSERT INTO tenants (id, name, slug, is_active, plan) "
        f"VALUES ('{_DEFAULT_TENANT_ID}', 'Default School', 'default', true, 'starter') "
        f"ON CONFLICT (id) DO NOTHING"
    ))

    # ── 4. Add tenant_id to users ───────────────────────────
    existing_user_cols = [c["name"] for c in inspector.get_columns("users")]
    if "tenant_id" not in existing_user_cols:
        op.add_column("users", sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_users_tenant_id", "users", "tenants", ["tenant_id"], ["id"],
            ondelete="CASCADE",
        )
        op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
        # Backfill: existing users belong to the default tenant
        op.execute(sa.text(
            f"UPDATE users SET tenant_id = '{_DEFAULT_TENANT_ID}' WHERE tenant_id IS NULL"
        ))
        # tenant_id stays NULLABLE — superadmin has NULL

    # ── 5. Add tenant_id to students ────────────────────────
    existing_student_cols = [c["name"] for c in inspector.get_columns("students")]
    if "tenant_id" not in existing_student_cols:
        op.add_column("students", sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_students_tenant_id", "students", "tenants", ["tenant_id"], ["id"],
            ondelete="CASCADE",
        )
        op.create_index("ix_students_tenant_id", "students", ["tenant_id"])
        # Backfill
        op.execute(sa.text(
            f"UPDATE students SET tenant_id = '{_DEFAULT_TENANT_ID}' WHERE tenant_id IS NULL"
        ))
        op.alter_column("students", "tenant_id", nullable=False)

    # ── 6. Drop old global unique on handle; replace with tenant-scoped one ──
    # Use plain SQL so a missing constraint never aborts the transaction.
    op.execute(sa.text(
        "ALTER TABLE students DROP CONSTRAINT IF EXISTS uq_students_handle"
    ))
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE students
                ADD CONSTRAINT uq_students_tenant_handle UNIQUE (tenant_id, handle);
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    op.drop_constraint("uq_students_tenant_handle", "students", type_="unique")
    op.drop_index("ix_students_tenant_id", table_name="students")
    op.drop_constraint("fk_students_tenant_id", "students", type_="foreignkey")
    op.drop_column("students", "tenant_id")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_constraint("fk_users_tenant_id", "users", type_="foreignkey")
    op.drop_column("users", "tenant_id")

    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
    op.execute(sa.text("DROP TYPE IF EXISTS tenant_plan CASCADE"))
