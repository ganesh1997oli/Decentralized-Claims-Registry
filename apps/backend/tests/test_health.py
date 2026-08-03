"""Operational health behavior must stay distinct and non-secret."""

from fastapi.testclient import TestClient

from apps.backend.app.health import ReadinessCheck, ReadinessProbe
from apps.backend.app.main import app, get_readiness_probe


def test_liveness_never_runs_external_dependencies():
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_every_successful_check():
    probe = ReadinessProbe(
        (
            ReadinessCheck("postgres", lambda: None, "database unavailable"),
            ReadinessCheck("sepolia", lambda: None, "contract unavailable"),
        )
    )
    app.dependency_overrides[get_readiness_probe] = lambda: probe
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "sepolia": "ok"},
    }


def test_readiness_returns_503_and_a_fixed_message_without_exception_text():
    def fail_with_sensitive_detail():
        raise OSError("postgresql://user:secret@example.invalid/database")

    probe = ReadinessProbe(
        (
            ReadinessCheck(
                "postgres",
                fail_with_sensitive_detail,
                "PostgreSQL is unavailable",
            ),
        )
    )
    app.dependency_overrides[get_readiness_probe] = lambda: probe
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": "PostgreSQL is unavailable"},
    }
    assert "secret" not in response.text


def test_readiness_rejects_duplicate_check_names():
    try:
        ReadinessProbe(
            (
                ReadinessCheck("postgres", lambda: None, "first"),
                ReadinessCheck("postgres", lambda: None, "second"),
            )
        )
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("Duplicate readiness names must be rejected")
