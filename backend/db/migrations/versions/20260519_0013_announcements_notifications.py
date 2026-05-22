"""add announcements and notifications tables

Revision ID: 0013_announcements_notifications
Revises: 0012_assignment_responses
Create Date: 2026-05-19

This migration uses raw SQL throughout so that SQLAlchemy's auto-create
behavior for postgres enums cannot trigger DuplicateObject errors when
re-running after a partial failure.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0013_announcements_notifications"
down_revision: str | None = "0012_assignment_responses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enum types (idempotent) ────────────────────────────────
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'announcement_audience') THEN
                CREATE TYPE announcement_audience AS ENUM
                    ('school', 'teachers', 'parents', 'students', 'classroom');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_kind') THEN
                CREATE TYPE notification_kind AS ENUM
                    ('announcement', 'mark_posted', 'attendance_alert',
                     'assignment_published', 'system');
            END IF;
        END $$;
        """
    )

    # ── announcements ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id            UUID PRIMARY KEY,
            tenant_id     UUID NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,
            author_id     UUID NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
            audience      announcement_audience NOT NULL,
            classroom_id  UUID          REFERENCES classrooms(id) ON DELETE CASCADE,
            title         VARCHAR(200) NOT NULL,
            body          TEXT NOT NULL,
            pinned        BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_announcements_tenant_id      ON announcements (tenant_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_announcements_author_id      ON announcements (author_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_announcements_classroom_id   ON announcements (classroom_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_announcements_tenant_created ON announcements (tenant_id, created_at);")

    # ── notifications ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id          UUID PRIMARY KEY,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind        notification_kind NOT NULL,
            title       VARCHAR(200) NOT NULL,
            body        TEXT NOT NULL,
            link        VARCHAR(500),
            payload     JSONB,
            read_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_user_id   ON notifications (user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_user_read ON notifications (user_id, read_at);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_user_read;")
    op.execute("DROP INDEX IF EXISTS ix_notifications_user_id;")
    op.execute("DROP TABLE IF EXISTS notifications;")

    op.execute("DROP INDEX IF EXISTS ix_announcements_tenant_created;")
    op.execute("DROP INDEX IF EXISTS ix_announcements_classroom_id;")
    op.execute("DROP INDEX IF EXISTS ix_announcements_author_id;")
    op.execute("DROP INDEX IF EXISTS ix_announcements_tenant_id;")
    op.execute("DROP TABLE IF EXISTS announcements;")

    op.execute("DROP TYPE IF EXISTS notification_kind;")
    op.execute("DROP TYPE IF EXISTS announcement_audience;")
