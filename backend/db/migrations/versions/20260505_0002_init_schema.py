"""init schema (all tables)

Revision ID: 0002_init_schema
Revises: 0001_enable_pgvector
Create Date: 2026-05-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002_init_schema"
down_revision: str | None = "0001_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VECTOR_DIM = 768


def upgrade() -> None:
    user_role = postgresql.ENUM("teacher", "student", "parent", name="user_role")
    user_role.create(op.get_bind(), checkfirst=True)

    attendance_status = postgresql.ENUM("present", "absent", "late", name="attendance_status")
    attendance_status.create(op.get_bind(), checkfirst=True)

    assessment_type = postgresql.ENUM(
        "homework", "unit_test", "exam", name="assessment_type"
    )
    assessment_type.create(op.get_bind(), checkfirst=True)

    question_difficulty = postgresql.ENUM(
        "easy", "medium", "hard", name="question_difficulty"
    )
    question_difficulty.create(op.get_bind(), checkfirst=True)

    upload_kind = postgresql.ENUM(
        "students", "attendance", "question_bank", "marks", name="upload_kind"
    )
    upload_kind.create(op.get_bind(), checkfirst=True)

    agent_run_status = postgresql.ENUM(
        "pending",
        "running",
        "completed",
        "insufficient_data",
        "validation_failed",
        "error",
        name="agent_run_status",
    )
    agent_run_status.create(op.get_bind(), checkfirst=True)

    report_status = postgresql.ENUM(
        "completed", "insufficient_data", "validation_failed", name="report_status"
    )
    report_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "students",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("handle", sa.String(64), nullable=False, unique=True),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("grade", sa.SmallInteger(), nullable=False),
        sa.Column("section", sa.String(8), nullable=False),
        sa.Column("parent_email", sa.String(254), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_students_handle", "students", ["handle"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "subjects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("subject_id", "name", name="uq_topics_subject_name"),
    )
    op.create_index("ix_topics_subject_id", "topics", ["subject_id"])

    op.create_table(
        "assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "type", postgresql.ENUM(name="assessment_type", create_type=False), nullable=False
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("assessment_date", sa.Date(), nullable=False),
        sa.Column("max_marks", sa.Numeric(8, 2), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_assessments_subject_date",
        "assessments",
        ["subject_id", "assessment_date"],
    )

    op.create_table(
        "questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question_no", sa.SmallInteger(), nullable=False),
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topics.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("max_marks", sa.Numeric(8, 2), nullable=False),
        sa.Column(
            "difficulty",
            postgresql.ENUM(name="question_difficulty", create_type=False),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "assessment_id", "question_no", name="uq_questions_assessment_no"
        ),
    )
    op.create_index("ix_questions_assessment_id", "questions", ["assessment_id"])
    op.create_index("ix_questions_topic_id", "questions", ["topic_id"])

    op.create_table(
        "question_responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("marks_obtained", sa.Numeric(8, 2), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "question_id", "student_id", name="uq_responses_question_student"
        ),
        sa.CheckConstraint("marks_obtained >= 0", name="ck_responses_marks_nonneg"),
    )
    op.create_index("ix_responses_question_id", "question_responses", ["question_id"])
    op.create_index("ix_responses_student_id", "question_responses", ["student_id"])

    op.create_table(
        "attendance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="attendance_status", create_type=False),
            nullable=False,
        ),
        sa.UniqueConstraint("student_id", "date", name="uq_attendance_student_date"),
    )
    op.create_index("ix_attendance_date", "attendance", ["date"])

    op.create_table(
        "uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "kind", postgresql.ENUM(name="upload_kind", create_type=False), nullable=False
        ),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("row_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_uploads_sha256", "uploads", ["sha256"])

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date_range_start", sa.Date(), nullable=False),
        sa.Column("date_range_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_run_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("graph_trace", postgresql.JSONB(), nullable=True),
        sa.Column(
            "triggered_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_agent_runs_student_started",
        "agent_runs",
        ["student_id", "started_at"],
    )

    op.create_table(
        "mentor_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status", postgresql.ENUM(name="report_status", create_type=False), nullable=False
        ),
        sa.Column("analysis_json", postgresql.JSONB(), nullable=False),
        sa.Column("insights_text", sa.Text(), nullable=False),
        sa.Column("insights_embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column("validation_json", postgresql.JSONB(), nullable=False),
        sa.Column("mentor_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_mentor_reports_student_created",
        "mentor_reports",
        ["student_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mentor_reports_student_created", table_name="mentor_reports")
    op.drop_table("mentor_reports")
    op.drop_index("ix_agent_runs_student_started", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_uploads_sha256", table_name="uploads")
    op.drop_table("uploads")
    op.drop_index("ix_attendance_date", table_name="attendance")
    op.drop_table("attendance")
    op.drop_index("ix_responses_student_id", table_name="question_responses")
    op.drop_index("ix_responses_question_id", table_name="question_responses")
    op.drop_table("question_responses")
    op.drop_index("ix_questions_topic_id", table_name="questions")
    op.drop_index("ix_questions_assessment_id", table_name="questions")
    op.drop_table("questions")
    op.drop_index("ix_assessments_subject_date", table_name="assessments")
    op.drop_table("assessments")
    op.drop_index("ix_topics_subject_id", table_name="topics")
    op.drop_table("topics")
    op.drop_table("subjects")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_students_handle", table_name="students")
    op.drop_table("students")

    for enum_name in (
        "report_status",
        "agent_run_status",
        "upload_kind",
        "question_difficulty",
        "assessment_type",
        "attendance_status",
        "user_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
