"""parent role enforcement + indexed phone_digits column

What this migration does:

1. Adds users.phone_digits — a normalized (digits-only) copy of users.phone,
   indexed for O(1) parent lookup by trailing 10 digits during login.
2. Backfills phone_digits for every existing user with a phone.
3. **Data repair**: every users row that is referenced as parent_students.parent_id
   but has role != 'parent' is upgraded to role='parent'. This fixes the seed-
   time anomaly where a household account was stored with role='student'.
4. Adds a trigger so future inserts into parent_students enforce that the
   parent_id row has role='parent' (defence in depth — the app already does
   this, but the trigger catches direct SQL / bad migrations).

Revision ID: 0019_parent_role_phone_digits
Revises: 0018_announcement_broadcast_id
Create Date: 2026-05-21
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0019_parent_role_phone_digits"
down_revision: str | None = "0018_announcement_broadcast_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. New column + index for fast phone lookup
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_digits VARCHAR(20);"
    )
    op.execute(
        "UPDATE users "
        "SET phone_digits = regexp_replace(phone, '[^0-9]', '', 'g') "
        "WHERE phone IS NOT NULL AND (phone_digits IS NULL OR phone_digits = '');"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_phone_digits ON users (phone_digits);"
    )

    # 2. Repair: any user that's referenced as parent_students.parent_id must have role='parent'
    op.execute(
        "UPDATE users SET role = 'parent' "
        "WHERE id IN (SELECT DISTINCT parent_id FROM parent_students) "
        "AND role != 'parent';"
    )

    # 3. Trigger: enforce role='parent' on parent_students.parent_id at insert/update.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_parent_role() RETURNS trigger AS $$
        DECLARE
            r text;
        BEGIN
            SELECT role::text INTO r FROM users WHERE id = NEW.parent_id;
            IF r IS NULL THEN
                RAISE EXCEPTION 'parent_students.parent_id % does not exist in users', NEW.parent_id;
            END IF;
            IF r != 'parent' THEN
                RAISE EXCEPTION 'parent_students.parent_id % has role=%, expected parent', NEW.parent_id, r;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_assert_parent_role ON parent_students;"
    )
    op.execute(
        "CREATE TRIGGER trg_assert_parent_role "
        "BEFORE INSERT OR UPDATE ON parent_students "
        "FOR EACH ROW EXECUTE FUNCTION assert_parent_role();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_assert_parent_role ON parent_students;")
    op.execute("DROP FUNCTION IF EXISTS assert_parent_role();")
    op.execute("DROP INDEX IF EXISTS ix_users_phone_digits;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS phone_digits;")
