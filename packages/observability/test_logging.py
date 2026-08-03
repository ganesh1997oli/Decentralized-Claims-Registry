"""Tests for the runtime JSON logging contract and secret boundary."""

import json
import logging
from io import StringIO

from packages.observability.logging import configure_logging, get_event_logger


def test_event_logger_emits_searchable_json_fields():
    output = StringIO()
    configure_logging("scoring-worker", stream=output)

    get_event_logger("test.worker").info(
        "claim.assessed",
        event_id="11155111:0xabc:0",
        claim_id=7,
        transaction_hash="0x123",
    )

    logged = json.loads(output.getvalue())
    assert logged == {
        "claim_id": 7,
        "event": "claim.assessed",
        "event_id": "11155111:0xabc:0",
        "logger": "test.worker",
        "message": "claim.assessed",
        "service": "scoring-worker",
        "severity": "INFO",
        "timestamp": logged["timestamp"],
        "transaction_hash": "0x123",
    }


def test_formatter_redacts_sensitive_keys_and_embedded_url_passwords():
    output = StringIO()
    configure_logging("claims-api", stream=output)

    get_event_logger("test.api").error(
        "dependency.failed",
        api_key="top-secret",
        database_url=(
            "postgresql://user:password@example.invalid/claims?sslpassword=query-secret"
        ),
        nested={"private-key": "also-secret"},
    )

    logged = json.loads(output.getvalue())
    assert logged["api_key"] == "[REDACTED]"
    assert logged["nested"]["private-key"] == "[REDACTED]"
    assert "user:password@" not in logged["database_url"]
    assert "=query-secret" not in logged["database_url"]


def test_unstructured_library_logs_still_use_the_json_envelope():
    output = StringIO()
    configure_logging("claims-api", stream=output, level=logging.WARNING)

    logging.getLogger("library").warning("adapter unavailable")

    logged = json.loads(output.getvalue())
    assert logged["event"] == "application.log"
    assert logged["message"] == "adapter unavailable"
