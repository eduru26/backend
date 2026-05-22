"""add chat tables

Revision ID: 0003_add_chat_tables
Revises: 0002_init_schema
Create Date: 2026-05-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "0003_add_chat_tables"
down_revision: str | None = "0002_init_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = inspector.get_table_names()

    # Idempotent enum creation — DO block swallows duplicate_object errors
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE message_role AS ENUM ('user', 'assistant');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # Reference the already-created enum without asking SQLAlchemy to create it
    message_role_col = sa.Column(
        "role",
        PgEnum(name="message_role", create_type=False),
        nullable=False,
    )

    if "conversations" not in existing:
        op.create_table(
            "conversations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    if "messages" not in existing:
        op.create_table(
            "messages",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "conversation_id",
                UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            message_role_col,
            sa.Column("content", sa.Text, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
    op.execute("DROP TYPE IF EXISTS message_role CASCADE")
