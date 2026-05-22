"""dedicated student user rows + handle login

What this migration does:

1. Adds users.handle — a unique, indexed identifier students use to log in
   (the student's roll number). Nullable because parents/teachers/admins
   don't have one.
2. Adds students.user_id — FK to the dedicated student user row. Unique so
   one User backs at most one Student, and ON DELETE SET NULL so deleting
   the User won't drop academic data (we soft-detach the link instead).
3. Backfills: for every Student that doesn't yet have a user_id, create a
   new users row with role='student', handle=student.handle,
   tenant_id=student.tenant_id, full_name=student.full_name, and
   password_hash copied from the first parent linked via parent_students
   (so the existing "log in with roll number + parent's password" flow
   keeps working — no user-visible reset).
4. Adds a trigger that enforces role='student' on users referenced by
   students.user_id, mirroring the role='parent' trigger from migration
   0019 (defence in depth against direct SQL).

The masquerade path in deps.py is dropped separately at the app layer.

Revision ID: 0020_student_user_handle
Revises: 0019_parent_role_phone_digits
Create Date: 2026-05-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020_student_user_handle"
down_revision: str | None = "0019_parent_role_phone_digits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. users.handle — nullable unique identifier for student logins.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS handle VARCHAR(64);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_handle "
        "ON users (handle) WHERE handle IS NOT NULL;"
    )

    # 2. students.user_id — FK to the student's own user row.
    op.execute(
        "ALTER TABLE students "
        "ADD COLUMN IF NOT EXISTS user_id UUID "
        "REFERENCES users(id) ON DELETE SET NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_user_id "
        "ON students (user_id) WHERE user_id IS NOT NULL;"
    )

    # 3. Backfill: create a student User for every Student that lacks one.
    # Copy password_hash from the first linked parent so the existing
    # roll-number+parent-password flow keeps working. Done as a row-by-row
    # DO block so a per-row unique-violation (e.g. the same handle already
    # taken by another tenant's student in this multi-tenant DB) is skipped
    # instead of aborting the entire migration.
    op.execute(
        """
        DO $$
        DECLARE
            s         RECORD;
            pw_hash   TEXT;
            new_uid   UUID;
        BEGIN
            FOR s IN
                SELECT id, tenant_id, handle, full_name
                FROM students
                WHERE user_id IS NULL
                ORDER BY created_at
            LOOP
                SELECT u.password_hash INTO pw_hash
                FROM parent_students ps
                JOIN users u ON u.id = ps.parent_id
                WHERE ps.student_id = s.id
                ORDER BY ps.created_at
                LIMIT 1;

                -- No linked parent → cannot derive a password; skip (the
                -- student row stays without a user_id and will be picked up
                -- next time a parent links / re-onboards).
                IF pw_hash IS NULL OR pw_hash = '' THEN
                    CONTINUE;
                END IF;

                -- Reuse an existing student user row for this tenant+handle
                -- if migration is being re-run or admin already created one.
                SELECT id INTO new_uid
                FROM users
                WHERE tenant_id = s.tenant_id
                  AND handle = s.handle
                  AND role = 'student'
                LIMIT 1;

                IF new_uid IS NULL THEN
                    BEGIN
                        INSERT INTO users (
                            id, tenant_id, handle, password_hash, role,
                            full_name, is_active, created_at
                        )
                        VALUES (
                            gen_random_uuid(), s.tenant_id, s.handle,
                            pw_hash, 'student', s.full_name, true, now()
                        )
                        RETURNING id INTO new_uid;
                    EXCEPTION WHEN unique_violation THEN
                        -- Cross-tenant handle clash. Don't fail the whole
                        -- migration; the admin can resolve by renaming
                        -- this student's roll number then re-onboarding.
                        new_uid := NULL;
                    END;
                END IF;

                IF new_uid IS NOT NULL THEN
                    UPDATE students SET user_id = new_uid WHERE id = s.id;
                END IF;
            END LOOP;
        END $$;
        """
    )

    # 4. Trigger: enforce role='student' on users referenced by
    # students.user_id. Mirrors the role='parent' trigger on parent_students.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_student_role() RETURNS trigger AS $$
        DECLARE
            r text;
        BEGIN
            IF NEW.user_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT role::text INTO r FROM users WHERE id = NEW.user_id;
            IF r IS NULL THEN
                RAISE EXCEPTION 'students.user_id % does not exist in users', NEW.user_id;
            END IF;
            IF r != 'student' THEN
                RAISE EXCEPTION 'students.user_id % has role=%, expected student', NEW.user_id, r;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_assert_student_role ON students;")
    op.execute(
        "CREATE TRIGGER trg_assert_student_role "
        "BEFORE INSERT OR UPDATE OF user_id ON students "
        "FOR EACH ROW EXECUTE FUNCTION assert_student_role();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_assert_student_role ON students;")
    op.execute("DROP FUNCTION IF EXISTS assert_student_role();")
    op.execute("DROP INDEX IF EXISTS uq_students_user_id;")
    op.execute("ALTER TABLE students DROP COLUMN IF EXISTS user_id;")
    op.execute("DROP INDEX IF EXISTS uq_users_handle;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS handle;")
