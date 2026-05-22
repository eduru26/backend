"""Integration tests for /lms/events/* — auth, payload validation, dispatch.

Each test starts a fresh FastAPI app with a custom Settings instance so we
don't depend on a real .env or a real Frappe site. The callback client is
patched out — these tests verify the inbound contract only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.lms_events import router
from backend.core.config import Settings, get_settings


SECRET = "s3cret-for-tests-only"


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(
        lms_inbound_secret=SECRET,
        lms_outbound_secret="outbound",
        lms_base_url="http://localhost:8080",
        lms_tenant_code="acme",
    )
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _patch_callback_client():
    """Avoid making real HTTP calls back to a Frappe site during tests."""
    with patch("backend.lms.client.LMSCallbackClient", autospec=True) as cls:
        instance = cls.return_value
        instance.ingest_insights = AsyncMock(return_value={"ok": True})
        instance.ingest_question_tags = AsyncMock(return_value={"ok": True})
        instance.ingest_risk_scores = AsyncMock(return_value={"ok": True})
        instance.append_message = AsyncMock(return_value={"ok": True})
        instance.aclose = AsyncMock()
        with patch("backend.lms.handlers.get_client", return_value=instance):
            yield instance


# ----- happy path ------------------------------------------------------------


def _post(client: TestClient, slug: str, payload: dict[str, Any], **headers: str):
    body = {"event": slug, "site": "acme.local", "ts": "2026-05-06T00:00:00Z", "payload": payload}
    return client.post(
        f"/lms/events/{slug}",
        json=body,
        headers={
            "authorization": f"Bearer {SECRET}",
            "x-tenant-code": "acme",
            **headers,
        },
    )


def test_quiz_submitted_accepted(client: TestClient, _patch_callback_client):
    payload = {
        "submission_id": "QSUB-2026-00001",
        "quiz": "intro-quiz",
        "course": "intro",
        "member": "alice@acme.com",
        "score": 7,
        "score_out_of": 10,
        "percentage": 70,
        "passing_percentage": 60,
        "passed": True,
        "results": [
            {"question": "Q1", "is_correct": 1, "marks": 1, "marks_out_of": 1, "answer": "A"},
            {"question": "Q2", "is_correct": 0, "marks": 0, "marks_out_of": 1, "answer": "B"},
        ],
    }
    resp = _post(client, "quiz_submitted", payload)
    assert resp.status_code == 202
    assert resp.json()["event"] == "quiz_submitted"
    assert resp.json()["tenant"] == "acme"


# ----- auth ------------------------------------------------------------------


def test_missing_bearer_rejected(client: TestClient):
    resp = client.post(
        "/lms/events/quiz_submitted",
        json={"event": "quiz_submitted", "payload": {}},
        headers={"x-tenant-code": "acme"},
    )
    assert resp.status_code == 401


def test_wrong_bearer_rejected(client: TestClient):
    resp = _post(client, "quiz_submitted", {}, authorization="Bearer wrong")
    assert resp.status_code == 401


def test_tenant_mismatch_rejected(client: TestClient):
    resp = _post(client, "quiz_submitted", {}, **{"x-tenant-code": "evilcorp"})
    assert resp.status_code == 400


# ----- shape -----------------------------------------------------------------


def test_unknown_event_404(client: TestClient):
    resp = _post(client, "made_up_event", {})
    assert resp.status_code == 404


def test_event_url_body_mismatch_400(client: TestClient):
    body = {"event": "quiz_submitted", "payload": {"submission_id": "X", "member": "a"}}
    resp = client.post(
        "/lms/events/lesson_completed",
        json=body,
        headers={"authorization": f"Bearer {SECRET}", "x-tenant-code": "acme"},
    )
    assert resp.status_code == 400


def test_invalid_payload_422(client: TestClient):
    # post_quiz_request requires submission_id
    resp = _post(client, "post_quiz_request", {})
    assert resp.status_code == 422


# ----- bridge unconfigured: 503 ---------------------------------------------


def test_unconfigured_bridge_returns_503():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(lms_inbound_secret="")
    c = TestClient(app)
    resp = c.post(
        "/lms/events/quiz_submitted",
        json={"event": "quiz_submitted", "payload": {}},
        headers={"authorization": "Bearer anything"},
    )
    assert resp.status_code == 503
