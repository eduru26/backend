from __future__ import annotations

import uuid

import pytest

from backend.db.models import UserRole
from backend.rag.guardrail import Guardrail
from backend.rag.identity import IdentityClaims
from backend.rag.intent import Classification, Intent


def _identity(role: UserRole) -> IdentityClaims:
    return IdentityClaims(
        role=role,
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        student_ids=(uuid.uuid4(),) if role in (UserRole.student, UserRole.parent) else (),
        classroom_ids=(uuid.uuid4(),) if role is UserRole.teacher else (),
        role_visibility=("public",),
    )


def _cls(intent: Intent, conf: float = 0.9) -> Classification:
    return Classification(intent=intent, confidence=conf, matched_rule="test")


# ── hard blocks ────────────────────────────────────────────


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("intent", [
    Intent.prompt_injection, Intent.pii_request,
    Intent.cross_student_query, Intent.off_topic,
])
def test_hard_blocks_apply_to_every_role(role: UserRole, intent: Intent) -> None:
    if role is UserRole.superadmin:
        pytest.skip("superadmin doesn't use the chat pipeline")
    gr = Guardrail()
    d = gr.evaluate(_cls(intent), _identity(role))
    assert d.allowed is False
    assert d.refusal_message


# ── policy matrix ───────────────────────────────────────────


def test_student_cannot_class_aggregate() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.class_aggregate), _identity(UserRole.student))
    assert d.allowed is False


def test_parent_cannot_class_aggregate() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.class_aggregate), _identity(UserRole.parent))
    assert d.allowed is False


def test_teacher_can_class_aggregate() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.class_aggregate), _identity(UserRole.teacher))
    assert d.allowed is True
    assert d.requires_structured is True


def test_school_admin_can_school_aggregate() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.school_aggregate), _identity(UserRole.school_admin))
    assert d.allowed is True


def test_student_can_own_performance() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.own_performance), _identity(UserRole.student))
    assert d.allowed is True


def test_parent_can_child_performance() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.child_performance), _identity(UserRole.parent))
    assert d.allowed is True


# ── retrieval gating ────────────────────────────────────────


def test_policy_qa_requires_rag() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.policy_qa), _identity(UserRole.student))
    assert d.allowed is True
    assert d.requires_rag is True
    # policy is knowledge-only — no structured fetch needed
    assert d.requires_structured is False


def test_own_performance_requires_structured_only() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.own_performance), _identity(UserRole.student))
    assert d.allowed is True
    assert d.requires_rag is False
    assert d.requires_structured is True


def test_announcement_recall_requires_rag_and_structured() -> None:
    gr = Guardrail()
    d = gr.evaluate(_cls(Intent.announcement_recall), _identity(UserRole.parent))
    assert d.allowed is True
    assert d.requires_rag is True
    # announcements may be intertwined with class-context, so structured remains on
    assert d.requires_structured is True
