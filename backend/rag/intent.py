from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Intent(str, enum.Enum):
    """A small, deterministic vocabulary of query intents.

    Each incoming chat message is mapped to exactly one of these by the
    rule-based classifier. The guardrail then consults a `(role, intent)`
    matrix to decide allow / block.

    Intents fall into three buckets:
      * Data intents — answered from the structured store
      * Knowledge intents — answered from the RAG corpus
      * Adversarial intents — blocked outright
    """

    # data intents
    own_performance = "own_performance"          # "where am I weak", "my marks"
    child_performance = "child_performance"      # parent asking about their child
    class_aggregate = "class_aggregate"          # teacher: "at-risk students"
    school_aggregate = "school_aggregate"        # school_admin: cross-class
    attendance_query = "attendance_query"        # any role, scoped to their student(s)

    # knowledge intents (RAG)
    curriculum_qa = "curriculum_qa"              # "what is the chapter on fractions"
    policy_qa = "policy_qa"                      # "what is the leave policy"
    announcement_recall = "announcement_recall"  # "what did the principal say last week"

    # control intents
    proactive_suggestion = "proactive_suggestion"  # "what should I focus on next"

    # adversarial / blocked
    cross_student_query = "cross_student_query"  # student/parent asking about other kids
    pii_request = "pii_request"                  # "give me their address / phone"
    prompt_injection = "prompt_injection"        # jailbreak attempts
    off_topic = "off_topic"                      # outside the mentor's scope


@dataclass(frozen=True)
class Classification:
    intent: Intent
    confidence: float          # 0..1; rule-based so usually 0.6 / 0.9 / 1.0
    matched_rule: str          # human-readable rule id for audit / logging
    target_student_handle: str | None = None  # extracted if user named another student


# ── rule patterns ───────────────────────────────────────────────


_INJECTION_PATTERNS = [
    r"ignore (all|the|previous|above)\s+(prior\s+)?instructions",
    r"disregard (the|your|all)\s+(system|previous|above)",
    r"system prompt",
    r"act as (?!.*mentor|.*tutor)",   # "act as a hacker" type prompts (allow act-as-mentor)
    r"jailbreak",
    r"developer mode",
    r"do anything now|\bDAN\b",
    r"reveal (the|your)\s+(system\s+)?prompt",
    r"print (the|your)\s+(system\s+)?prompt",
]

_PII_PATTERNS = [
    r"\b(home|residential)\s+address\b",
    r"\bphone\s+number\b",
    r"\bdate\s+of\s+birth\b|\bdob\b",
    r"\baadhaar\b|\bssn\b|\bpan\s+(card|number)\b",
    r"\bmother'?s?\s+(maiden|name)\b",
    r"\bpassword\b|\bpasscode\b",
    r"\bemail\s+address\b",
]

_PERFORMANCE_KEYWORDS = [
    "weak", "weakness", "strong", "score", "scored", "marks", "grade",
    "improve", "performance", "result", "test", "exam", "subject", "topic",
    "doing in", "how am i", "how is", "summary",
]

_ATTENDANCE_KEYWORDS = ["attendance", "absent", "absence", "present", "leaves", "missed"]

_CLASS_KEYWORDS = [
    "class", "classroom", "students", "section", "at-risk", "at risk",
    "struggling", "top performers", "average", "overall",
]

_SCHOOL_AGG_KEYWORDS = [
    "school", "grade-wide", "across grades", "all classes", "overall school",
    "teacher performance", "all teachers",
]

_CURRICULUM_KEYWORDS = [
    "chapter", "lesson", "syllabus", "curriculum", "topic on", "explain",
    "what is", "definition", "concept",
]

_POLICY_KEYWORDS = [
    "policy", "rule", "guideline", "leave policy", "fee", "uniform",
    "code of conduct", "discipline",
]

# Phrasings where the user is asking a policy *question* even though they
# never used the word "policy". These are checked before attendance so
# "how many leaves am I allowed" routes to policy_qa, not attendance_query.
_POLICY_PHRASES = [
    re.compile(r"\bhow many\s+leaves?\b"),
    re.compile(r"\bleaves?\s+(allowed|permitted|allowance|limit)\b"),
    re.compile(r"\bleave\s+(allowance|limit|rule|policy)\b"),
    re.compile(r"\bminimum\s+(passing|attendance)\b"),
    re.compile(r"\b(re-?exam|re-?examination)\b"),
    re.compile(r"\b(school|class)\s+(rule|guideline)s?\b"),
]

_ANNOUNCEMENT_KEYWORDS = [
    "announcement", "circular", "notice", "what did the principal",
    "what did the teacher say", "memo", "post from",
]

_PROACTIVE_KEYWORDS = [
    "what should i", "what should my child", "focus next", "study next",
    "recommend", "suggestion", "next step", "where to start",
]

_OFF_TOPIC_KEYWORDS = [
    "weather", "joke", "recipe", "movie", "song", "stock", "crypto", "bitcoin",
    "president", "election", "war",
]

# `S-1042`, `STU001`, `2024XYZ` — a rough handle/roll-number shape. Used
# only to detect "talk about another student" — the actual access check is
# in the guardrail, this is just classification.
_HANDLE_RE = re.compile(r"\b([A-Z]{1,4}-?\d{2,6}[A-Z]?)\b")


class IntentClassifier:
    """Deterministic, rule-based classifier.

    Trade-off vs an LLM classifier: zero added latency, fully auditable,
    no extra failure mode. Accuracy on this small intent space is high
    because the vocabulary is narrow (school context, ~10 intents).
    Ambiguous cases fall back to `own_performance` for students/parents
    and `class_aggregate` for teachers — both safe defaults since the
    structured pipeline is the most common path.
    """

    def classify(self, question: str, role: str) -> Classification:
        q = (question or "").strip()
        if not q:
            return Classification(Intent.off_topic, 1.0, "empty")

        ql = q.lower()

        # 1. Adversarial first — fail closed.
        for pat in _INJECTION_PATTERNS:
            if re.search(pat, ql):
                return Classification(Intent.prompt_injection, 1.0, f"injection:{pat}")
        for pat in _PII_PATTERNS:
            if re.search(pat, ql):
                return Classification(Intent.pii_request, 1.0, f"pii:{pat}")

        # 2. Cross-student detection for student/parent.
        target_handle: str | None = None
        m = _HANDLE_RE.search(q)
        if m:
            target_handle = m.group(1)
        if role in ("student", "parent"):
            # naive heuristic: third-party pronouns + a name-shape OR an
            # explicit handle that the guardrail will check against the
            # caller's student_ids
            if target_handle is not None:
                return Classification(
                    Intent.cross_student_query, 0.9,
                    "cross_student:handle_mentioned",
                    target_student_handle=target_handle,
                )
            if re.search(r"\b(other|another|the other|all|every)\s+(student|kid|child|children)\b", ql):
                return Classification(Intent.cross_student_query, 0.8, "cross_student:keyword")

        # 3. Knowledge intents.
        if _any_in(ql, _ANNOUNCEMENT_KEYWORDS):
            return Classification(Intent.announcement_recall, 0.8, "announcement_kw")
        if _any_in(ql, _POLICY_KEYWORDS):
            return Classification(Intent.policy_qa, 0.8, "policy_kw")
        for pat in _POLICY_PHRASES:
            if pat.search(ql):
                return Classification(Intent.policy_qa, 0.85, f"policy_phrase:{pat.pattern}")
        if _any_in(ql, _CURRICULUM_KEYWORDS) and not _any_in(ql, _PERFORMANCE_KEYWORDS):
            return Classification(Intent.curriculum_qa, 0.7, "curriculum_kw")

        # 4. Control intents.
        if _any_in(ql, _PROACTIVE_KEYWORDS):
            return Classification(Intent.proactive_suggestion, 0.8, "proactive_kw")

        # 5. Data intents.
        if _any_in(ql, _ATTENDANCE_KEYWORDS):
            return Classification(Intent.attendance_query, 0.9, "attendance_kw")

        if role == "school_admin" and _any_in(ql, _SCHOOL_AGG_KEYWORDS):
            return Classification(Intent.school_aggregate, 0.9, "school_agg_kw")

        if role == "teacher" and _any_in(ql, _CLASS_KEYWORDS):
            return Classification(Intent.class_aggregate, 0.85, "class_agg_kw")

        if role == "parent" and (target_handle is None):
            # parent asking "how is my child doing" — child_performance, not own.
            if re.search(r"\b(my child|my kid|my son|my daughter|child'?s?)\b", ql) \
                    or _any_in(ql, _PERFORMANCE_KEYWORDS):
                return Classification(Intent.child_performance, 0.85, "child_perf_kw")

        if role == "student" and _any_in(ql, _PERFORMANCE_KEYWORDS):
            return Classification(Intent.own_performance, 0.85, "own_perf_kw")

        if _any_in(ql, _OFF_TOPIC_KEYWORDS):
            return Classification(Intent.off_topic, 0.8, "off_topic_kw")

        # 6. Safe fallback per role.
        fallback = {
            "student": Intent.own_performance,
            "parent": Intent.child_performance,
            "teacher": Intent.class_aggregate,
            "school_admin": Intent.school_aggregate,
        }.get(role, Intent.off_topic)
        return Classification(fallback, 0.4, "fallback")


def _any_in(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)
