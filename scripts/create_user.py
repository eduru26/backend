"""Bootstrap utility: create a user.

Examples:
    python -m scripts.create_user --email teacher@example.com --password secret --role teacher
    python -m scripts.create_user --email parent@example.com --password secret --role parent --student-handle S-1042
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.exc import IntegrityError

from backend.core.logging import configure_logging, get_logger
from backend.core.security import hash_password
from backend.db.base import dispose_engine, get_session_factory
from backend.db.models import UserRole
from backend.db.repositories.students import StudentRepository
from backend.db.repositories.users import UserRepository

logger = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in UserRole],
    )
    parser.add_argument("--full-name", default=None)
    parser.add_argument(
        "--student-handle",
        default=None,
        help="Required for student/parent role: link to an existing student",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    role = UserRole(args.role)
    factory = get_session_factory()
    async with factory() as session:
        student_id = None
        if role in (UserRole.student, UserRole.parent):
            if not args.student_handle:
                logger.error("create_user.missing_handle", role=role.value)
                return 2
            students = StudentRepository(session)
            student = await students.get_by_handle(args.student_handle)
            if student is None:
                logger.error("create_user.unknown_student", handle=args.student_handle)
                return 3
            student_id = student.id

        users = UserRepository(session)
        try:
            user = await users.create(
                email=args.email,
                password_hash=hash_password(args.password),
                role=role,
                full_name=args.full_name,
                student_id=student_id,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.error("create_user.email_exists", email=args.email)
            return 4

        logger.info(
            "create_user.ok",
            user_id=str(user.id),
            email=user.email,
            role=user.role.value,
        )
        return 0


async def _main() -> int:
    configure_logging()
    args = _parse_args()
    try:
        return await _run(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
