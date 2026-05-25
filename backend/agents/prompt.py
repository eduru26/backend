from __future__ import annotations

from typing import Any

from backend.agents.state import MentorState

_SYSTEM: dict[str, str] = {
    "student": (
        "You are an AI academic mentor speaking directly to a student. Help them understand "
        "their performance using ONLY the data and retrieved context provided. Be encouraging, "
        "specific, and actionable. Never invent numbers, topics, names, or facts not present in "
        "the data. When you cite a retrieved passage, reference it as [#N]. Keep your response "
        "under 200 words."
    ),
    "parent": (
        "You are an AI academic advisor speaking to a parent about their child. Use ONLY the "
        "data and retrieved context provided. Be clear, constructive, and suggest concrete actions "
        "the parent can take. Never invent numbers, topics, or facts. Never mention other "
        "children's names or scores. When you cite a retrieved passage, reference it as [#N]. "
        "Keep your response under 200 words."
    ),
    "teacher": (
        "You are an AI teaching assistant helping a teacher analyse class or student performance "
        "using ONLY the data and retrieved context provided. Be analytical, identify patterns, "
        "and suggest targeted interventions. Never invent numbers, topics, or facts. When you "
        "cite a retrieved passage, reference it as [#N]. Keep your response under 300 words."
    ),
    "school_admin": (
        "You are an AI advisor to a school administrator. "
        "Answer the administrator's question directly and factually using ONLY the DATA CONTEXT provided. "
        "The DATA CONTEXT contains the exact list of teachers, students, and grades enrolled at the school — "
        "trust it completely. If the DATA CONTEXT shows teachers, name them and their details. "
        "Never say 'there are no teachers' or 'no data' when the DATA CONTEXT lists them. "
        "Be crisp: answer what was asked, nothing more. "
        "Do not name individual students unless explicitly asked. "
        "Never invent numbers, names, or facts not present in the data. "
        "When you cite a retrieved passage, reference it as [#N]. "
        "Keep your response under 150 words."
    ),
}


class PromptAgent:
    def run(self, state: MentorState) -> dict[str, Any]:
        role = state["role"]
        analytics = state.get("analytics") or {}
        students = state.get("students") or []
        question = state["question"]

        system = _SYSTEM.get(role, _SYSTEM["student"])

        sections: list[str] = []

        # 1. structured data context (only present if ContextAgent ran)
        if state.get("requires_structured", True):
            sid = state.get("student_id")
            if role == "school_admin" and not sid:
                structured = _admin_context(
                    state.get("teachers") or [],
                    state.get("students") or [],
                    analytics,
                )
            elif role == "teacher" and not sid:
                structured = _teacher_context(analytics)
            else:
                student = next(
                    (s for s in students if s["id"] == sid),
                    students[0] if students else {},
                )
                structured = _student_context(student, analytics)
            if structured:
                sections.append(f"DATA CONTEXT:\n{structured}")

        # 2. knowledge graph block (already pre-rendered)
        kg_block = (state.get("kg_block") or "").strip()
        if kg_block:
            sections.append(kg_block)

        # 3. retrieved RAG chunks with citation IDs — or an explicit
        # "no material found" notice when retrieval was attempted but
        # came back empty. This is the difference between "the model
        # has nothing to ground on" (where it should say "I couldn't
        # find this in our materials") and "this is a structured-data
        # query so RAG was never asked" (where the model should answer
        # from the structured block alone).
        chunks = state.get("rag_chunks") or []
        if chunks:
            rag_lines = [
                "RETRIEVED CONTEXT (cite as [#N]; do NOT emit [#N] unless "
                "you are quoting from one of these passages):"
            ]
            for i, c in enumerate(chunks, start=1):
                title = c.get("title") or c.get("kind") or "source"
                kind = c.get("kind") or "doc"
                body = (c.get("content") or "").strip()
                if len(body) > 600:
                    body = body[:597] + "…"
                rag_lines.append(f"[#{i}] ({kind}) {title}\n{body}")
            sections.append("\n\n".join(rag_lines))
        elif state.get("requires_rag"):
            sections.append(
                "RETRIEVED CONTEXT: No relevant material was found in the "
                "school's documents for this question. Tell the user you "
                "couldn't find it rather than guessing — do not invent a "
                "policy or curriculum answer."
            )

        # 4. conversation memory (last N turns)
        mem_block = (state.get("memory_block") or "").strip()
        if mem_block:
            sections.append(mem_block)

        # 5. the live question
        sections.append(f"QUESTION: {question}")

        user = "\n\n".join(sections)
        return {"system_prompt": system, "user_prompt": user}


def _student_context(student: dict[str, Any], analytics: dict[str, Any]) -> str:
    lines: list[str] = []
    if student:
        lines.append(
            f"Student: {student.get('name', 'Unknown')}, "
            f"Grade {student.get('grade')}{student.get('section', '')}"
        )
    if analytics.get("overall_pct") is not None:
        lines.append(f"Overall score: {analytics.get('overall_pct', 'N/A')}%")
    if analytics.get("subject_pct"):
        lines.append(
            "Subject scores: "
            + ", ".join(f"{s}: {p}%" for s, p in analytics["subject_pct"].items())
        )
    if analytics.get("weak_chapters"):
        lines.append("Weak chapters (below 60%): " + ", ".join(analytics["weak_chapters"][:4]))
    if analytics.get("weak_topics"):
        lines.append("Weak topics (below 60%): " + ", ".join(analytics["weak_topics"][:5]))
    if analytics.get("attendance_rate") is not None:
        lines.append(f"Attendance rate: {analytics['attendance_rate']}%")
    if not analytics.get("total_questions_attempted"):
        lines.append("Note: No assessment data has been uploaded yet.")
    return "\n".join(lines)


def _admin_context(
    teachers: list[dict[str, Any]],
    students: list[dict[str, Any]],
    analytics: dict[str, Any],
) -> str:
    lines: list[str] = []

    # ── Teacher roster ───────────────────────────────────────
    lines.append(f"TEACHER ROSTER: {len(teachers)} teacher(s) enrolled.")
    for i, t in enumerate(teachers, 1):
        row = f"  {i}. {t['name']}"
        if t.get("specialization"):
            row += f" | Specialization: {t['specialization']}"
        assignments = t.get("subject_assignments") or []
        hc = t.get("home_classroom")
        if assignments:
            grades = "; ".join(
                f"Grade {a['grade']}{a['section']} — {a['subject']}" for a in assignments
            )
            row += f" | Assigned grades: {grades}"
        elif hc:
            row += f" | Assigned grade: Grade {hc['grade']}{hc['section']} ({hc['name']})"
        else:
            row += " | No classroom assigned yet"
        lines.append(row)

    # ── Student roster ───────────────────────────────────────
    student_count = len(students)
    lines.append(f"\nSTUDENT ROSTER: {student_count} student(s) enrolled.")
    _MAX_STUDENTS = 50  # cap prompt size for large schools
    for i, s in enumerate(students[:_MAX_STUDENTS], 1):
        lines.append(
            f"  {i}. {s['name']} | Grade {s['grade']}{s.get('section', '')}"
        )
    if student_count > _MAX_STUDENTS:
        lines.append(f"  ... and {student_count - _MAX_STUDENTS} more students.")

    # ── Performance aggregate (only if data exists) ──────────
    at_risk = analytics.get("at_risk_students") or []
    if at_risk:
        names = ", ".join(f"{s['name']} ({s['overall_pct']}%)" for s in at_risk[:5])
        lines.append(f"\nAt-risk students ({len(at_risk)}): {names}")
    if analytics.get("class_subject_avg"):
        lines.append(
            "School subject averages: "
            + ", ".join(f"{s}: {p}%" for s, p in analytics["class_subject_avg"].items())
        )

    return "\n".join(lines)


def _teacher_context(analytics: dict[str, Any]) -> str:
    lines: list[str] = [f"Class size: {analytics.get('student_count', 0)} students"]
    if analytics.get("class_subject_avg"):
        lines.append(
            "Class subject averages: "
            + ", ".join(f"{s}: {p}%" for s, p in analytics["class_subject_avg"].items())
        )
    at_risk = analytics.get("at_risk_students", [])
    if at_risk:
        names = ", ".join(f"{s['name']} ({s['overall_pct']}%)" for s in at_risk[:5])
        lines.append(f"At-risk students ({len(at_risk)}): {names}")
    else:
        lines.append("No at-risk students identified.")
    return "\n".join(lines)
