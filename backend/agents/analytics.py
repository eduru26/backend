from __future__ import annotations

from typing import Any

from backend.agents.state import MentorState


def _pct(obtained: float, maximum: float) -> float:
    return round(obtained / maximum * 100, 1) if maximum > 0 else 0.0


class AnalyticsAgent:
    def run(self, state: MentorState) -> dict[str, Any]:
        role = state["role"]
        scores = state["raw_scores"]
        attendance = state["raw_attendance"]
        sid = state.get("student_id")

        # Teacher / school_admin with no student focused → class-level report
        if role in ("teacher", "school_admin") and not sid:
            return {"analytics": self._class_analytics(state["students"], scores, attendance)}

        # Student, parent, or teacher focused on one student → per-student report
        s_scores = [r for r in scores if r["student_id"] == sid] if sid else scores
        s_att = [r for r in attendance if r["student_id"] == sid] if sid else attendance
        return {"analytics": self._student_analytics(s_scores, s_att)}

    # ── student-level ───────────────────────────────────────

    def _student_analytics(
        self, scores: list[dict[str, Any]], attendance: list[dict[str, Any]]
    ) -> dict[str, Any]:
        subj: dict[str, dict[str, float]] = {}
        topic: dict[str, dict[str, float]] = {}
        chapter: dict[str, dict[str, float]] = {}

        for r in scores:
            s, t = r["subject"], r["topic"]
            subj.setdefault(s, {"obt": 0.0, "max": 0.0})
            subj[s]["obt"] += r["marks_obtained"]
            subj[s]["max"] += r["max_marks"]
            key = f"{s} > {t}"
            topic.setdefault(key, {"obt": 0.0, "max": 0.0})
            topic[key]["obt"] += r["marks_obtained"]
            topic[key]["max"] += r["max_marks"]
            # Chapter rollup — only for topics linked into a curriculum tree.
            ch = r.get("chapter")
            if ch:
                ckey = f"{s} > {ch}"
                chapter.setdefault(ckey, {"obt": 0.0, "max": 0.0})
                chapter[ckey]["obt"] += r["marks_obtained"]
                chapter[ckey]["max"] += r["max_marks"]

        subject_pct = {s: _pct(v["obt"], v["max"]) for s, v in subj.items()}
        topic_pct = {k: _pct(v["obt"], v["max"]) for k, v in topic.items()}
        chapter_pct = {k: _pct(v["obt"], v["max"]) for k, v in chapter.items()}
        weak_topics = sorted(
            [k for k, v in topic_pct.items() if v < 60.0],
            key=lambda k: topic_pct[k],
        )
        weak_chapters = sorted(
            [k for k, v in chapter_pct.items() if v < 60.0],
            key=lambda k: chapter_pct[k],
        )

        total_obt = sum(v["obt"] for v in subj.values())
        total_max = sum(v["max"] for v in subj.values())
        overall = _pct(total_obt, total_max)

        total_att = len(attendance)
        present = sum(1 for a in attendance if a["status"] == "present")
        att_rate: float | None = _pct(present, total_att) if total_att > 0 else None

        return {
            "overall_pct": overall,
            "subject_pct": subject_pct,
            "topic_pct": topic_pct,
            "chapter_pct": chapter_pct,
            "weak_topics": weak_topics,
            "weak_chapters": weak_chapters,
            "attendance_rate": att_rate,
            "total_questions_attempted": len(scores),
            "total_attendance_days": total_att,
        }

    # ── class-level ─────────────────────────────────────────

    def _class_analytics(
        self,
        students: list[dict[str, Any]],
        scores: list[dict[str, Any]],
        attendance: list[dict[str, Any]],
    ) -> dict[str, Any]:
        per_student: dict[str, dict[str, Any]] = {}
        for st in students:
            sid = st["id"]
            s_scores = [r for r in scores if r["student_id"] == sid]
            s_att = [r for r in attendance if r["student_id"] == sid]
            a = self._student_analytics(s_scores, s_att)
            per_student[sid] = {"name": st["name"], **a}

        at_risk = [
            {
                "id": sid,
                "name": d["name"],
                "overall_pct": d["overall_pct"],
                "attendance_rate": d["attendance_rate"],
            }
            for sid, d in per_student.items()
            if d["overall_pct"] < 50.0
            or (d["attendance_rate"] is not None and d["attendance_rate"] < 75.0)
        ]
        at_risk.sort(key=lambda x: x["overall_pct"])

        class_subj: dict[str, list[float]] = {}
        for d in per_student.values():
            for subj, pct in d["subject_pct"].items():
                class_subj.setdefault(subj, []).append(pct)
        class_subject_avg = {
            s: round(sum(vals) / len(vals), 1) for s, vals in class_subj.items()
        }

        return {
            "student_count": len(students),
            "at_risk_students": at_risk,
            "class_subject_avg": class_subject_avg,
            "per_student": per_student,
        }
