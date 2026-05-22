# LMS Analysis — Eduru Integration & White-Label Readiness

> Source codebase: `./lms-main` (Frappe LMS, app name `frappe_lms`).
> Scope: read-only analysis. No code in `lms-main` is modified.
> Status: Phase 1–3 of Eduru integration analysis complete. SaaS / white-label redesign pending approval.

---

## 1. LMS Architecture Summary

**Framework**: Frappe app (`frappe_lms`). Two registered modules in [lms/modules.txt](../lms-main/lms/modules.txt): **LMS** and **Job**.

**Backend (Python / Frappe)** — [lms-main/lms/](../lms-main/lms/):
- [hooks.py](../lms-main/lms/hooks.py) — single registry of doc events, schedulers, permissions, jinja, route rules.
- [lms/api.py](../lms-main/lms/lms/api.py) — ~2,383 lines, **63 whitelisted endpoints** (the public REST surface used by the SPA).
- [lms/utils.py](../lms-main/lms/lms/utils.py) — ~2,400 lines of helpers (progress recalculation, permissions, role checks, content macros).
- [lms/doctype/](../lms-main/lms/lms/doctype/) — **70 DocTypes**.
- [job/](../lms-main/lms/job/) — separate "careers" sub-module (Job Opportunity, Job Application).

**Frontend** — [frontend/src/](../lms-main/frontend/src/):
- Vue 3 SPA mounted at `/lms` (route prefix configurable via `lms_path` site config — see [hooks.py:18-22](../lms-main/lms/hooks.py#L18-L22)).
- Built on `frappe-ui`. Talks to backend through `createDocumentResource` / `createResource` calls against `/api/method/lms.lms.api.*`.
- Key pages in [pages/](../lms-main/frontend/src/pages): `Home/{StudentHome,AdminHome}`, `Courses/`, `Batches/`, `Lesson.vue`, `QuizPage.vue`, `QuizSubmission.vue`, `Statistics.vue`, `Profile.vue`.

**Persistence**: Frappe DocTypes (MariaDB tables) + a SQLite full-text search index registered via `sqlite_search` hook ([sqlite.py](../lms-main/lms/sqlite.py)).

---

## 2. Key Entities & Data Model

Verified from the JSON DocType definitions:

| Entity (DocType) | Key fields | Purpose |
|---|---|---|
| **User** (core Frappe) | `email`, `full_name`, `username`, `user_image` | Identity. Extended via `LMS Settings` and `LMS Enrollment.member_type` (Student/Mentor/Staff). |
| **LMS Course** | `title`, `instructors[]`, `category`, `chapters[Chapter Reference]`, `enable_certification`, `paid_course`, `published`, `enrollments`, `lessons`, `rating` | Course root. Stats fields auto-updated hourly. |
| **Course Chapter** + **Chapter Reference** | `chapter`, `course`, `idx`, `is_scorm_package` | Chapters and their ordering inside a course (parent table on Course). |
| **Course Lesson** + **Lesson Reference** | `title`, `chapter`, `body` (Markdown w/ macros), `content` (block JSON), `youtube`, `quiz_id`, `question` (assignment) | Lesson body. **Quizzes/assignments live as inline blocks/macros within lesson content** — see `get_quiz_progress()` in [course_lesson.py:188-219](../lms-main/lms/lms/doctype/course_lesson/course_lesson.py#L188-L219). |
| **LMS Quiz** | `title`, `questions[LMS Quiz Question]`, `lesson`, `course`, `passing_percentage`, `max_attempts`, `shuffle_questions`, `enable_negative_marking`, `duration` | Quiz config. |
| **LMS Question** | `question`, `type` (Choices / User Input / Open Ended), `option_1..4` + `is_correct_1..4` + `explanation_1..4`, `possibility_1..4` | Question bank. **No topic/difficulty/skill tagging.** |
| **LMS Quiz Submission** | `quiz`, `member`, `score`, `score_out_of`, `percentage`, `passing_percentage`, `result[LMS Quiz Result]` | One row per attempt. |
| **LMS Quiz Result** (child) | `question`, `question_name` → `LMS Question`, `answer`, `is_correct`, `marks`, `marks_out_of` | Per-question result. **Has `is_correct`, has link back to question — gold for AI weakness detection.** |
| **LMS Assignment** + **LMS Assignment Submission** | `assignment`, `member`, `answer`, `assignment_attachment`, `status` (Pass/Fail/Not Graded), `evaluator`, `comments`, `course`, `lesson` | Assignment workflow. Manual grading. |
| **LMS Enrollment** | `member`, `course`, `progress` (Float %), `current_lesson`, `payment`, `purchased_certificate`, `certificate`, `enrollment_from_batch` | The student↔course join, with cached progress %. |
| **LMS Course Progress** | `member`, `lesson`, `chapter`, `course`, `status` (Complete / Partially Complete / Incomplete), `is_scorm_chapter`, `scorm_content` | One row per (member, lesson). Drives heatmap + completion %. |
| **LMS Batch** + **LMS Batch Enrollment** | `title`, `start_date`, `end_date`, `seat_count`, `courses[Batch Course]`, `assessment`, `timetable[LMS Batch Timetable]`, `instructors`, `paid_batch`, `certification`, `conferencing_provider` (Zoom/Google Meet) | Cohort/class-batch — multi-course bundle with schedule. |
| **LMS Program** + **LMS Program Course** + **LMS Program Member** | `program_courses[]`, `program_members[]`, `enforce_course_order` | Curriculum/track. |
| **LMS Live Class** + **LMS Live Class Participant** | `title`, `date`, `time`, `start_url`, `join_url`, attendance via scheduler hourly | Synchronous classes. **Attendance IS tracked** via `lms.lms.doctype.lms_live_class.lms_live_class.update_attendance` ([hooks.py:142](../lms-main/lms/hooks.py#L142)). |
| **LMS Certificate** + **LMS Certificate Request** + **LMS Certificate Evaluation** | `issue_date`, `course`, `member`, `evaluator`, `template` (Print Format) | Issuance + evaluation pipeline. |
| **LMS Programming Exercise** + **LMS Test Case** + **LMS Programming Exercise Submission** + **LMS Test Case Submission** | `code`, `test_cases[]`, pass/fail per case | Code-execution exercises. **Per-test-case results** — gold for code-skill profiling. |
| **LMS Video Watch Duration** | `lesson`, video, watched seconds | Engagement signal — **per-video time tracking already exists** (`track_video_watch_duration` in [api.py:1656](../lms-main/lms/lms/api.py#L1656)). |
| **LMS Lesson Note** | `lesson`, `member`, `content` | Student-authored notes per lesson. |
| **LMS Course Review** | rating + text | Course feedback. |
| **LMS Badge** + **LMS Badge Assignment** | rule-based badges | Auto-assigned by `process_badges` on **every** `on_change` ([hooks.py:117-119](../lms-main/lms/hooks.py#L117-L119)) — already a global event hook we can piggyback on. |
| **LMS Course Mentor Mapping** | `mentor`, `member`, `course` | **Existing mentor↔mentee link — natural place for Eduru.** |
| **LMS Settings** (single) | branding, sidebar, payment, signup, sidebar items, persona | App-level config. |
| **LMS Category** / **Skills** / **Function** / **Industry** | Taxonomies | Mostly used for course classification + user persona. |

**Key relationships** (verified):
- `LMS Course` 1—N `Chapter Reference` → `Course Chapter` 1—N `Lesson Reference` → `Course Lesson`
- `Course Lesson` ↔ `LMS Quiz` (linked via `quiz_id` on lesson, populated bidirectionally in `save_lesson_details_in_quiz`, [course_lesson.py:55-69](../lms-main/lms/lms/doctype/course_lesson/course_lesson.py#L55-L69))
- `LMS Quiz Submission` 1—N `LMS Quiz Result` → `LMS Question`
- `LMS Enrollment` ←—— `User` × `LMS Course`
- `LMS Course Progress` ←—— `User` × `Course Lesson`

---

## 3. Feature List (verified)

- **Course authoring** — chapters, lessons (Markdown + block-JSON), embedded quizzes/assignments/programming exercises/videos/PDFs/audio/SCORM packages via macro renderers ([hooks.py:250-259](../lms-main/lms/hooks.py#L250-L259)).
- **Course delivery** — paid/free, USD-equivalent pricing, coupons, GST, currency.
- **Quizzes** — MCQ (single + multi-correct), text input (fuzzy match via `fuzzywuzzy`, threshold 85 — [lms_quiz.py:316](../lms-main/lms/lms/doctype/lms_quiz/lms_quiz.py#L316)), open-ended (manually graded), shuffle, max attempts, negative marking, duration, passing %.
- **Assignments** — Document/PDF/URL/Image/Text submissions, manual evaluator grading.
- **Programming exercises** — code submissions vs test cases.
- **Progress tracking** — per-lesson `LMS Course Progress`; aggregated `progress %` cached on `LMS Enrollment`; recalculated on lesson add/delete via background queue ([course_lesson.py:27-43](../lms-main/lms/lms/doctype/course_lesson/course_lesson.py#L27-L43)).
- **Batches (cohorts)** — scheduled multi-course program with timetable, seat count, instructors, conferencing.
- **Live Classes** — Zoom + Google Meet integration, attendance pulled hourly.
- **Certificates** — auto issuance, evaluator-led requests, custom Print Format templates.
- **Discussions** — Discussion Topic + Reply, with notification + @mention hooks.
- **Notifications** — Notification Log, real-time via `frappe.publish_realtime`, in-app + email.
- **Engagement analytics** — heatmap (`get_heatmap_data`, [api.py:1152](../lms-main/lms/lms/api.py#L1152)), streak (`get_streak_info`, [api.py:1802](../lms-main/lms/lms/api.py#L1802)), video watch duration, course progress distribution.
- **Course statistics** — hourly job `update_course_statistics` writes enrollments/lessons/rating onto course doc.
- **Mentor mapping** — `LMS Course Mentor Mapping` already exists.
- **Jobs board** — Job Opportunity / Job Application sub-module.
- **Badges** — rule-based, auto-assigned on any doc change.
- **Reviews** — `LMS Course Review`.
- **SCORM** — full SCORM-package chapter support with progress capture.
- **Payments** — depends on `frappe/payments`; `LMS Payment` doctype + reminders + coupons.
- **Persona capture** — onboarding questionnaire (`capture_user_persona`, [api.py:1482](../lms-main/lms/lms/api.py#L1482)).

---

## 4. Data Flow (text diagram)

```
┌─────────────┐
│   User      │  signs up → Frappe User (User.after_insert → lms.user.after_insert)
└─────┬───────┘
      │ enroll (POST /api/method/lms.lms.api... or LMS Enrollment.insert)
      ▼
┌─────────────────────────┐      payment (optional)         ┌─────────────┐
│   LMS Enrollment        │◄────────────────────────────────│ LMS Payment │
│  member, course,        │                                 └─────────────┘
│  progress=0, current_lesson=null
└─────┬───────────────────┘
      │ user opens /lms/courses/<slug>/<chapter>/<lesson>
      ▼
┌─────────────────────────┐
│   Course Lesson (body)  │ — markdown macros / blocks parsed (md.find_macros, plugins.quiz_renderer)
└─────┬───────────────────┘
      │ video → track_video_watch_duration → LMS Video Watch Duration
      │ inline {{ Quiz: <id> }} or block "quiz"
      ▼
┌─────────────────────────┐
│   QuizPage.vue          │ user answers
└─────┬───────────────────┘
      │ POST /api/method/lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz
      ▼
┌─────────────────────────┐
│ submit_quiz()           │ process_results() → verify_answer per question
│  – auto-grade Choices/Input
│  – Open Ended → flagged for manual grading
└─────┬───────────────────┘
      │ create_submission()
      ▼
┌─────────────────────────┐        ┌─────────────────────────┐
│  LMS Quiz Submission    │ 1───N  │  LMS Quiz Result        │
│  score, percentage      │        │  question, answer,      │
└─────┬───────────────────┘        │  is_correct, marks      │
      │                            └─────────────────────────┘
      │ if percentage >= passing → save_progress(lesson, course)
      ▼
┌─────────────────────────┐
│  LMS Course Progress    │  status=Complete (only if all quizzes+assignments in lesson done)
└─────┬───────────────────┘
      │ enrollment.progress = recalculated %
      │ enrollment.save() → on_change → process_badges
      │ frappe.publish_realtime("update_lesson_progress")
      ▼
   Front-end UI updates progress bar + next lesson pointer

Assignments flow (parallel):
LMS Assignment → AssignmentSubmission.vue → LMS Assignment Submission (status="Not Graded")
                                            → evaluator grades → status=Pass/Fail
                                            → save_progress fires same way
```

Hourly schedulers: `update_course_statistics`, `update_attendance`, `mark_eval_as_completed`, `schedule_evals`. Daily: published-course/batch reminders + payment reminders.

---

## 5. AI Opportunity Mapping

| LMS Feature | Data Available | Eduru Use Case |
|---|---|---|
| `LMS Quiz Result` (`is_correct`, `marks`, `question_name` link, `answer`) | Per-question correctness across attempts | **Topic-level weakness detection**, mistake clustering, distractor analysis (which wrong option was chosen) |
| `LMS Quiz Submission.creation` × `passing_percentage` | Pass/fail trajectory over time | Trend detection, regression alerts |
| `LMS Course Progress.status` over time | Lesson completions w/ timestamps | **Drop-off detection**, pace estimation, predicted completion date |
| `LMS Enrollment.progress`, `current_lesson` | Position in course | Stuck-on-lesson detection (`current_lesson` not advancing) |
| `LMS Video Watch Duration` (per-video seconds) | Engagement minutes per lesson video | Skim detection, re-watch detection → engagement score |
| `LMS Assignment Submission.comments` + `status` | Evaluator written feedback | **RAG corpus for personalised feedback**; auto-summarise feedback themes per student |
| `LMS Programming Exercise Submission` + `LMS Test Case Submission` (per-test-case pass/fail) | Skill matrix at code level | Per-skill code-competency profile; suggest missing test cases |
| `LMS Live Class Participant` (attendance, hourly) | Synchronous engagement | **Attendance-vs-performance correlation**; predict at-risk after N missed sessions |
| `LMS Discussion Topic` / `Reply` | Free-text questions | Auto-answer Q&A from course content; cluster FAQs to surface to instructor |
| `LMS Course Review` ratings + free text | Course quality + sentiment | Identify confusing chapters (low rating + high quiz failure on same chapter) |
| `LMS Lesson Note` | Student notes | Personalized recap; flashcard generation per student |
| `LMS Quiz Submission` aggregated by question | Per-question difficulty in the wild | Heat-map for instructor: which question + which distractor trips students; flag broken questions |
| `LMS Course` chapter/lesson body (Markdown) | Course content corpus | RAG over course material to ground AI answers (not hallucinate) |
| Heatmap (`fetch_activity_data`) | Activity dates across 3 doctypes | Irregular-study-pattern detection |
| Streak (`get_streak_info`) | Daily activity continuity | Motivation nudges |
| Persona (`capture_user_persona` → `LMS Settings.persona_captured`) | Self-declared goals/level | Personalize recommendations |

### AI Use-Case Categories

**A. Performance Analysis** — `LMS Quiz Result`-driven topic weakness (needs topic tagging — see gaps); per-attempt trend; distractor analysis.

**B. Behavioral Analysis** — `LMS Course Progress` cadence; `LMS Video Watch Duration` skim ratio; long gaps between `current_lesson` updates → drop-off risk.

**C. Attendance** — `LMS Live Class Participant` already captured hourly; correlate `attendance%` × `quiz pass rate` × `progress %`.

**D. Predictive Insights** — Risk-of-failure model: features = (attendance %, avg quiz %, days since last activity, lessons completed / expected by today, video skim ratio). Likely-weak-subject = predicted next-quiz score by topic.

**E. Personalization** — Study plan from gap = (incomplete lessons) ∪ (failed quizzes) ∪ (weak topics from result clustering). Practice question generation grounded in `LMS Question` + course body.

---

## 6. AI Touchpoints in UI

### Student-side
1. **Post-quiz screen** — [frontend/src/pages/QuizPage.vue](../lms-main/frontend/src/pages/QuizPage.vue): after `submit_quiz`, render an "Eduru analysis" panel (weak topics + recommended next lesson + 3 practice questions). Trigger: response of `submit_quiz`.
2. **Student dashboard** — [frontend/src/pages/Home/StudentHome.vue](../lms-main/frontend/src/pages/Home/StudentHome.vue): add an "AI Insights" card above the existing `UpcomingEvaluations` / `myCourses` blocks: weekly summary, at-risk warning, suggested 15-min focus.
3. **Lesson page** — [frontend/src/pages/Lesson.vue](../lms-main/frontend/src/pages/Lesson.vue) + [components/LessonContent.vue](../lms-main/frontend/src/components/LessonContent.vue): "Ask Eduru about this lesson" sidebar (RAG over lesson body + chapter context).
4. **Course page** — [frontend/src/pages/Courses/CourseDetail.vue](../lms-main/frontend/src/pages/Courses/CourseDetail.vue): "Are you ready for this?" pre-flight readiness check using prior course history.
5. **Profile** — [frontend/src/pages/Profile.vue](../lms-main/frontend/src/pages/Profile.vue) + [components/StudentHeatmap.vue](../lms-main/frontend/src/components/StudentHeatmap.vue): "AI summary of your learning month".
6. **Quiz Submission view** — [frontend/src/pages/QuizSubmission.vue](../lms-main/frontend/src/pages/QuizSubmission.vue): per-question explanation rewrite + analogy.

### Instructor-side
7. **Course Dashboard** — [frontend/src/pages/Courses/CourseDashboard.vue](../lms-main/frontend/src/pages/Courses/CourseDashboard.vue) + the existing `get_course_progress_distribution` and `get_lesson_completion_stats` APIs: add a **"topic-difficulty heatmap"** card (which question/topic has lowest class-wide accuracy).
8. **Student Progress** — [frontend/src/pages/Courses/StudentCourseProgress.vue](../lms-main/frontend/src/pages/Courses/StudentCourseProgress.vue): per-student AI risk badge + suggested intervention.
9. **Quiz Submission grading** — [QuizSubmission.vue](../lms-main/frontend/src/pages/QuizSubmission.vue): AI draft grade + rubric for **open-ended** answers (currently 100% manual — see [lms_quiz.py:169-174](../lms-main/lms/lms/doctype/lms_quiz/lms_quiz.py#L169-L174)).
10. **Assignment grading** — [AssignmentSubmission.vue](../lms-main/frontend/src/pages/AssignmentSubmission.vue): AI suggested feedback for evaluator.

### Admin-side
11. **Statistics** — [frontend/src/pages/Statistics.vue](../lms-main/frontend/src/pages/Statistics.vue): platform-wide cohort risk distribution, predicted completion rate, content gaps (lessons with high enrolment + low completion).
12. **Course authoring** — [frontend/src/pages/Courses/CourseForm.vue](../lms-main/frontend/src/pages/Courses/CourseForm.vue) + [QuizForm.vue](../lms-main/frontend/src/pages/QuizForm.vue): AI suggests question tags, generates distractors, flags low-quality questions (those everyone gets right or wrong).

---

## 7. Trigger Points for AI Pipeline

Concrete event hooks that should fire AI tasks (verified against [hooks.py](../lms-main/lms/hooks.py) and existing controllers):

| Trigger | Where | Action |
|---|---|---|
| Quiz submitted | end of `submit_quiz()` in [lms_quiz.py:130](../lms-main/lms/lms/doctype/lms_quiz/lms_quiz.py#L130), OR `LMS Quiz Submission` on `after_insert` doc-event | Enqueue: per-question topic tagging (if missing), update student weakness vector, generate post-quiz feedback |
| Assignment graded | `LMS Assignment Submission.on_update` when `status` changes from `Not Graded` | Generate personalised feedback summary; update writing-skill profile |
| Lesson completed | `LMS Course Progress.after_insert` with `status=Complete` (created in `save_progress`, [course_lesson.py:96-104](../lms-main/lms/lms/doctype/course_lesson/course_lesson.py#L96-L104)) | Mastery update; recompute "next best lesson"; if Programming Exercise — skill matrix |
| Enrollment created | `LMS Enrollment.after_insert` | Pre-flight readiness check, baseline diagnostic suggestion |
| Progress changes | the existing global `on_change` hook (`process_badges` already piggy-backs here, [hooks.py:117-119](../lms-main/lms/hooks.py#L117-L119)) | Cheap re-eval of risk score (debounce required) |
| Live class attendance update | hourly scheduler `update_attendance` ([hooks.py:142](../lms-main/lms/hooks.py#L142)) | Update at-risk-from-attendance flag |
| Daily batch | new scheduler entry (after `daily` block in [hooks.py:144-150](../lms-main/lms/hooks.py#L144-L150)) | (a) Batch risk scoring for all active enrollments. (b) Engagement digest. (c) Re-cluster question difficulty. |
| Weekly summary | new scheduler entry `weekly` | Per-student email/in-app weekly report. |
| Notification publish | existing `Notification Log.on_change` hook | Optional: AI-personalised notification copy. |

---

## 8. Data Gaps & Fixes

Critical gaps that block strong AI insights — and the cheapest way to derive each:

| Gap | Why it matters | Cheapest fix |
|---|---|---|
| **No topic / subtopic / skill tag on `LMS Question`** ([lms_question.json](../lms-main/lms/lms/doctype/lms_question/lms_question.json) has only `question`, `type`, options, possibilities) | Cannot do topic-level weakness detection without it | Two paths: (a) Add a custom `topics` Table MultiSelect linking to existing `Skills` doctype; (b) AI-derive at ingest time by classifying `question` text against course chapter list — store result in a new `LMS Question Topic` table. **Prefer (b) augmenting (a) — auto-tag, instructor confirms.** |
| **No difficulty level on questions** | Cannot do adaptive practice or fairness-adjusted scoring | Derive from observed correctness: `1 − (correct_count / attempt_count)` once a question has ≥ N=30 attempts; store as `LMS Question Statistics` doctype. |
| **No per-question time-spent** | Skim vs deliberate = strong signal | Frontend-side: add `started_at`/`answered_at` to each `LMS Quiz Result` payload; the `submit_quiz` controller can persist them as new columns. |
| **No error classification on Open Ended** | Manual feedback only | Plug AI grader at `LMS Quiz Result` row save when `type == "Open Ended"` — not as a replacement, but suggestion to evaluator. |
| **Attendance is per-class only — no per-lesson attendance** | But `LMS Course Progress.creation` + `LMS Video Watch Duration` already tell you who showed up to async content. | Define "engagement attendance" = lesson opened within N days of expected schedule; derive in analytics layer, no schema change. |
| **No "expected pace" baseline** | Can't say "behind schedule" without one | For batch enrollments, derive from `LMS Batch Timetable` (already exists — `LMS Batch.timetable`). For self-paced, use cohort median. |
| **No standardised "topic" entity** | Skills exist but not consistently linked to course/chapter/question | Add `topic` link on `Course Chapter` (single Link to existing `Skills` or new `LMS Topic`) — minimal one-field extension. |
| **No long-form student profile / preferences** | AI can't personalize tone | Already partially solved by `capture_user_persona`/`persona_captured` in `LMS Settings` — just expose to AI service. |
| **No "AI feedback / recommendation" storage** | Cannot show history, cannot evaluate AI quality | Will need new doctype (next phase): `Eduru Insight` (member, context_doctype, context_name, kind, payload JSON, created_at, ack_at). |

All of these are **additive** — none requires modifying existing DocType fields.

---

## 9. Integration Readiness Summary

### Extension points (already provided by the LMS — no fork needed)

- **`hooks.py.doc_events`** is the canonical place to attach AI triggers. The wildcard `"*": { "on_change": [...] }` slot is already in use ([hooks.py:115-130](../lms-main/lms/hooks.py#L115-L130)) — your AI listener can sit alongside.
- **`scheduler_events`** ([hooks.py:134-151](../lms-main/lms/hooks.py#L134-L151)) — drop in `daily` and `weekly` AI batch jobs without changes to existing tasks.
- **`@frappe.whitelist()`** — Eduru exposes its own endpoints under a new module (e.g. `lms.ai_mentor.api.*`); the SPA can call them via the same `createResource` pattern as other APIs.
- **`override_whitelisted_methods`** ([hooks.py:163-165](../lms-main/lms/hooks.py#L163-L165)) — currently empty; if we ever need to *augment* (not replace) `submit_quiz`'s response, prefer subscribing to a doc-event and calling a separate endpoint from the frontend rather than overriding.
- **Realtime channel** — `frappe.publish_realtime("update_lesson_progress")` already exists. AI insights can be pushed via a parallel `ai_mentor_insight` event.
- **Frontend extension** — Vue SPA is monolithic; AI panels are added as new components consumed inside existing pages (no fork of `frappe-ui`).
- **`profile_tabs` hook** ([hooks.py:233](../lms-main/lms/hooks.py#L233)) — exists specifically for plugging tabs into user profile via `lms.lms.plugins.ProfileTab` subclass. Natural slot for "Eduru" tab on student profile.
- **`lms_lesson_page_extensions` hook** ([hooks.py:240-242](../lms-main/lms/hooks.py#L240-L242)) — exists for adding scripts/styles to lesson pages. Slot for an in-lesson chat widget.
- **`lms_markdown_macro_renderers`** ([hooks.py:250-259](../lms-main/lms/hooks.py#L250-L259)) — already extensible. We could add a `{{ AIPrompt: ... }}` macro for instructor-authored AI prompts inside lessons.
- **`extend_bootinfo`** ([hooks.py:228-230](../lms-main/lms/hooks.py#L228-L230)) — already used; Eduru can inject feature flags / model status into bootinfo here.

### Safe integration strategy

1. **Create a new sibling app** `ai_mentor` (Frappe app) — *not* a folder inside `lms`. It declares `required_apps = ["frappe_lms"]`, registers its own `hooks.py`. Frappe loads both; events from LMS doctypes are subscribed to from the AI app's `hooks.py.doc_events`. **Zero modification of `lms-main`.**
2. **All AI-derived data goes into AI-Mentor-owned doctypes** (`Eduru Insight`, `AI Question Tag`, `AI Risk Score`, `AI Conversation`, `AI Conversation Message`). LMS doctypes stay pristine.
3. **Read-only access to LMS data** via `frappe.db.get_all` / `frappe.get_doc` — Frappe permissions cover this.
4. **Frontend integration** is via *new* Vue components imported into LMS pages through frappe-ui slots, OR through a small bundle (`ai_mentor.bundle.js`) injected via `web_include_js` from the AI app's hooks. If we don't want to touch the SPA at all initially, ship a separate `/ai-mentor` desk page that opens in a side panel.
5. **Keep all AI requests async**: `frappe.enqueue` to the `long` queue (the LMS already uses this pattern in `recalculate_progress`). Quiz submission must not be blocked by an LLM call.
6. **Untouched zones**: `lms.lms.api`, all DocType JSONs under `lms-main/lms/lms/doctype/`, `frappe-ui` package, `cypress/` tests, `hooks.py` — none of these need edits for Phase 1 AI integration.

---

## Phase Status

- ✅ Phase 1 — Codebase understanding (architecture, entities, features, data flow).
- ✅ Phase 2 — Eduru opportunity mapping (data sources, use-case categories, UI touchpoints, triggers).
- ✅ Phase 3 — Integration readiness (extension points, safe strategy, data gaps).
- ⏸ **Pending approval** — SaaS / white-label redesign:
  - SaaS gap analysis (hardcoded branding, single-tenant assumptions, isolation gaps).
  - White-label architecture (multi-tenancy model, Tenant entity, branding, feature flags).
  - Implementation plan (model changes, UI customization, API design, migration plan).
