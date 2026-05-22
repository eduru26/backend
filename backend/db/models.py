from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.config import get_settings
from backend.db.base import Base

VECTOR_DIM = get_settings().vector_dim


# ─── Enums ───────────────────────────────────────────────


SUBJECTS: list[str] = [
    "English", "Kannada", "Hindi", "Mathematics", "Social Science",
    "EVS", "Science", "Physics", "Chemistry", "Biology", "Computer Science",
]


class UserRole(str, enum.Enum):
    superadmin = "superadmin"
    school_admin = "school_admin"
    teacher = "teacher"
    student = "student"
    parent = "parent"


class TenantPlan(str, enum.Enum):
    starter = "starter"
    pro = "pro"
    enterprise = "enterprise"


class AttendanceStatus(str, enum.Enum):
    present = "present"
    absent = "absent"
    late = "late"


class AssessmentType(str, enum.Enum):
    homework = "homework"
    unit_test = "unit_test"
    exam = "exam"


class QuestionDifficulty(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class UploadKind(str, enum.Enum):
    students = "students"
    attendance = "attendance"
    question_bank = "question_bank"
    marks = "marks"


class AgentRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    insufficient_data = "insufficient_data"
    validation_failed = "validation_failed"
    error = "error"


class ReportStatus(str, enum.Enum):
    completed = "completed"
    insufficient_data = "insufficient_data"
    validation_failed = "validation_failed"


# Reusable column helpers
def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ─── Tables ──────────────────────────────────────────────


class Tenant(Base):
    """One row = one school."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )
    plan: Mapped[TenantPlan] = mapped_column(
        Enum(TenantPlan, name="tenant_plan", native_enum=True),
        nullable=False,
        default=TenantPlan.starter,
        server_default=TenantPlan.starter.value,
    )
    # School logo as a data URI (e.g. "data:image/png;base64,..."), <=500 KB
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    users: Mapped[list[User]] = relationship(back_populates="tenant")
    students: Mapped[list[Student]] = relationship(back_populates="tenant")
    classrooms: Mapped[list[Classroom]] = relationship(back_populates="tenant")


class Student(Base):
    __tablename__ = "students"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    handle: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    grade: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    section: Mapped[str] = mapped_column(String(8), nullable=False)
    parent_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Dedicated User row this student logs in as (role='student', handle =
    # this student's roll number). One-to-one. Nullable for older rows that
    # haven't been migrated yet; new students always get one in onboarding.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("tenant_id", "handle", name="uq_students_tenant_handle"),
    )

    tenant: Mapped[Tenant] = relationship(back_populates="students")
    classroom: Mapped[Classroom | None] = relationship(back_populates="students")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    attendance: Mapped[list[Attendance]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    responses: Mapped[list[QuestionResponse]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,  # NULL = superadmin (no school affiliation)
        index=True,
    )
    # email is nullable — parents log in via phone number instead
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True, index=True)
    # Digit-only normalized copy of `phone` for indexed lookup during login.
    # Kept in sync at the app layer (UserRepository) — see _normalize_phone.
    phone_digits: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Login identifier for student users — the student's roll number. Unique
    # when present (enforced by a partial unique index in migration 0020).
    # Nullable because parents/teachers/admins don't have one.
    handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True),
        nullable=False,
    )
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    specialization: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = _created_at()

    tenant: Mapped[Tenant | None] = relationship(back_populates="users")


class Classroom(Base):
    """A class group within a school, assigned to one teacher."""

    __tablename__ = "classrooms"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_classrooms_tenant_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    grade: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    section: Mapped[str] = mapped_column(String(8), nullable=False)
    teacher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = _created_at()

    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")

    tenant: Mapped[Tenant] = relationship(back_populates="classrooms")
    teacher: Mapped[User | None] = relationship(foreign_keys=[teacher_id])
    students: Mapped[list[Student]] = relationship(back_populates="classroom")
    history: Mapped[list[StudentClassHistory]] = relationship(back_populates="classroom")


class TeacherSubjectAssignment(Base):
    """A teacher teaches their subject in a specific classroom (may span multiple grades)."""

    __tablename__ = "teacher_subject_assignments"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id", "classroom_id", "subject",
            name="uq_tsa_teacher_cls_subject",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    classroom_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    teacher: Mapped[User] = relationship(foreign_keys=[teacher_id])
    classroom: Mapped[Classroom] = relationship()


class ParentStudent(Base):
    """Many-to-many: one parent account can be linked to multiple students."""

    __tablename__ = "parent_students"
    __table_args__ = (
        UniqueConstraint("parent_id", "student_id", name="uq_parent_students"),
    )

    id: Mapped[uuid.UUID] = _pk()
    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = _created_at()

    parent: Mapped[User] = relationship(foreign_keys=[parent_id])
    student: Mapped[Student] = relationship()


class TimetableEntry(Base):
    """One period slot in a teacher's weekly timetable."""

    __tablename__ = "timetable_entries"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id", "day_of_week", "period_number",
            name="uq_timetable_slot",
        ),
        Index(
            "uq_timetable_classroom_slot",
            "classroom_id", "day_of_week", "period_number",
            unique=True,
            postgresql_where=text("classroom_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)    # 1=Mon … 6=Sat
    period_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1-8
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    teacher: Mapped[User] = relationship(foreign_keys=[teacher_id])
    classroom: Mapped[Classroom | None] = relationship()


class PeriodTime(Base):
    """Per-school bell schedule: maps each period number to a start/end time."""

    __tablename__ = "period_times"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "day_type", "period_number",
            name="uq_period_times_tenant_daytype_period",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # "weekday" (Mon-Fri) or "saturday" — schools run a different Saturday bell schedule
    day_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="weekday",
    )
    period_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class AcademicYear(Base):
    """One row = one school year (e.g. 2025-2026). Only one is_current per tenant."""

    __tablename__ = "academic_years"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_academic_years_tenant_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(20), nullable=False)   # e.g. "2025-2026"
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = _created_at()

    tenant: Mapped[Tenant] = relationship()
    history: Mapped[list[StudentClassHistory]] = relationship(back_populates="academic_year")


class StudentClassHistory(Base):
    """Audit trail: which classroom was each student in for each academic year."""

    __tablename__ = "student_class_history"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "academic_year_id", name="uq_class_history_student_year"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    classroom_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("academic_years.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    enrolled_at: Mapped[datetime] = _created_at()
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    student: Mapped[Student] = relationship()
    classroom: Mapped[Classroom] = relationship(back_populates="history")
    academic_year: Mapped[AcademicYear] = relationship(back_populates="history")


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    topics: Mapped[list[Topic]] = relationship(
        back_populates="subject", cascade="all, delete-orphan"
    )


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("subject_id", "name", name="uq_topics_subject_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(VECTOR_DIM),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()

    subject: Mapped[Subject] = relationship(back_populates="topics")


class Assessment(Base):
    __tablename__ = "assessments"
    __table_args__ = (
        Index("ix_assessments_subject_date", "subject_id", "assessment_date"),
    )

    id: Mapped[uuid.UUID] = _pk()
    type: Mapped[AssessmentType] = mapped_column(
        Enum(AssessmentType, name="assessment_type", native_enum=True),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)
    max_marks: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()

    questions: Mapped[list[Question]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("assessment_id", "question_no", name="uq_questions_assessment_no"),
    )

    id: Mapped[uuid.UUID] = _pk()
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    max_marks: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    difficulty: Mapped[QuestionDifficulty] = mapped_column(
        Enum(QuestionDifficulty, name="question_difficulty", native_enum=True),
        nullable=False,
    )

    assessment: Mapped[Assessment] = relationship(back_populates="questions")
    responses: Mapped[list[QuestionResponse]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class QuestionResponse(Base):
    __tablename__ = "question_responses"
    __table_args__ = (
        UniqueConstraint("question_id", "student_id", name="uq_responses_question_student"),
        CheckConstraint("marks_obtained >= 0", name="ck_responses_marks_nonneg"),
    )

    id: Mapped[uuid.UUID] = _pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    marks_obtained: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    is_correct: Mapped[bool] = mapped_column(nullable=False, default=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    question: Mapped[Question] = relationship(back_populates="responses")
    student: Mapped[Student] = relationship(back_populates="responses")


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("student_id", "date", name="uq_attendance_student_date"),
        Index("ix_attendance_date", "date"),
    )

    id: Mapped[uuid.UUID] = _pk()
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus, name="attendance_status", native_enum=True),
        nullable=False,
    )

    student: Mapped[Student] = relationship(back_populates="attendance")


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = _pk()
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    kind: Mapped[UploadKind] = mapped_column(
        Enum(UploadKind, name="upload_kind", native_enum=True),
        nullable=False,
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    uploaded_at: Mapped[datetime] = _created_at()
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_student_started", "student_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    date_range_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_range_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status", native_enum=True),
        nullable=False,
        default=AgentRunStatus.pending,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    graph_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    triggered_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )


class MentorReport(Base):
    __tablename__ = "mentor_reports"
    __table_args__ = (
        Index("ix_mentor_reports_student_created", "student_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status", native_enum=True),
        nullable=False,
    )
    analysis_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    insights_text: Mapped[str] = mapped_column(Text, nullable=False)
    insights_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(VECTOR_DIM), nullable=True
    )
    validation_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mentor_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


# ─── Chat ────────────────────────────────────────────────


class AssignmentStatus(str, enum.Enum):
    draft = "draft"
    published = "published"


class Assignment(Base):
    """MCQ assignment published by a teacher to a classroom (holds multiple questions)."""

    __tablename__ = "assignments"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    classroom_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, name="assignment_status", native_enum=True),
        nullable=False, default=AssignmentStatus.draft, server_default="draft",
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    teacher: Mapped[User] = relationship(foreign_keys=[teacher_id])
    classroom: Mapped[Classroom] = relationship()
    questions: Mapped[list[AssignmentQuestion]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan",
    )


class AssignmentQuestion(Base):
    """One MCQ question within an assignment."""

    __tablename__ = "assignment_questions"

    id: Mapped[uuid.UUID] = _pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_num: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    option_a: Mapped[str] = mapped_column(String(500), nullable=False)
    option_b: Mapped[str] = mapped_column(String(500), nullable=False)
    option_c: Mapped[str] = mapped_column(String(500), nullable=False)
    option_d: Mapped[str] = mapped_column(String(500), nullable=False)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)  # a|b|c|d

    assignment: Mapped[Assignment] = relationship(back_populates="questions")


class AssignmentResponse(Base):
    """One student's submission for an assignment."""

    __tablename__ = "assignment_responses"
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", name="uq_asgn_response"),
    )

    id: Mapped[uuid.UUID] = _pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    total_questions: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    assignment: Mapped[Assignment] = relationship()
    student: Mapped[Student] = relationship()


# ─── Announcements & Notifications ───────────────────────


class AnnouncementAudience(str, enum.Enum):
    school = "school"        # everyone in tenant
    teachers = "teachers"    # all teachers
    parents = "parents"      # all parents
    students = "students"    # all students
    classroom = "classroom"  # one classroom (students + their parents)


class Announcement(Base):
    """Posts from school admin or teachers to a targeted audience."""

    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    audience: Mapped[AnnouncementAudience] = mapped_column(
        Enum(AnnouncementAudience, name="announcement_audience", native_enum=True),
        nullable=False,
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    # Set when this row is part of a multi-tenant superadmin broadcast — all
    # tenant copies share the same broadcast_id so the admin app can list and
    # delete them as one logical post.
    broadcast_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = _created_at()

    author: Mapped[User] = relationship(foreign_keys=[author_id])
    classroom: Mapped[Classroom | None] = relationship()


class NotificationKind(str, enum.Enum):
    announcement = "announcement"
    mark_posted = "mark_posted"
    attendance_alert = "attendance_alert"
    assignment_published = "assignment_published"
    class_reminder = "class_reminder"
    system = "system"


class Notification(Base):
    """Per-user notifications inbox."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, name="notification_kind", native_enum=True),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


# ─── Chat continued ──────────────────────────────────────


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = _created_at()

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role", native_enum=True),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ─── RAG (retrieval-augmented generation) ────────────────


from sqlalchemy.dialects.postgresql import ARRAY  # noqa: E402 — keep RAG block self-contained


class RagDocumentKind(str, enum.Enum):
    curriculum = "curriculum"
    policy = "policy"
    announcement = "announcement"
    assignment = "assignment"
    note = "note"
    upload = "upload"


class RagDocument(Base):
    """A logical source ingested into the RAG corpus.

    The `role_visibility` array drives RBAC: a chunk derived from this
    document is only retrievable by callers whose `role_visibility`
    overlaps with this array (PostgreSQL `&&` operator on text[]).
    Idempotency is enforced by `(tenant_id, sha256)`.
    """

    __tablename__ = "rag_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="uq_rag_documents_tenant_sha"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role_visibility: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{public}",
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"),
        nullable=True,
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=True,
    )
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(150), nullable=True)
    document_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}",
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()


class RagDocumentChunk(Base):
    """One searchable chunk of a RagDocument.

    Metadata is denormalised from the parent for filter-before-search:
    the retriever pre-narrows on `(tenant_id, role_visibility &&, ...)`
    so HNSW only scores survivors.
    """

    __tablename__ = "rag_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_rag_chunks_doc_idx"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rag_documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(VECTOR_DIM), nullable=True,
    )
    role_visibility: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{public}",
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"),
        nullable=True,
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=True,
    )
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(150), nullable=True)
    chunk_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}",
    )
    created_at: Mapped[datetime] = _created_at()


class RagQueryEvent(Base):
    """One row per chat turn — drives analytics and proactive suggestions.

    We hash the question (sha256) so we can group trends without storing
    free text twice (the `messages` table already has the raw text).
    """

    __tablename__ = "rag_query_events"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    intent: Mapped[str] = mapped_column(String(40), nullable=False)
    blocked: Mapped[bool] = mapped_column(nullable=False, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rag_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
