"""Verify one deployment-specific Kafka identity reaches every GCP service."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "infrastructure" / "gcp" / "compose.yml"
ENV_EXAMPLE = PROJECT_ROOT / "infrastructure" / "gcp" / ".env.gcp.example"
VERIFY_SCRIPT = PROJECT_ROOT / "infrastructure" / "gcp" / "scripts" / "verify-deployment.sh"
DASHBOARD = PROJECT_ROOT / "infrastructure" / "gcp" / "monitoring" / "dashboard.json"
DEPLOY_SCRIPT = PROJECT_ROOT / "infrastructure" / "gcp" / "scripts" / "deploy.sh"


def _compose_model(**overrides: str) -> dict[str, object]:
    """Resolve Compose exactly as deployment would, with optional test values."""

    environment = os.environ.copy()
    environment.update(overrides)
    completed = subprocess.run(
        (
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _command_text(service: dict[str, object]) -> str:
    command = service["command"]
    if isinstance(command, list):
        return " ".join(str(argument) for argument in command)
    return str(command)


def _assert_kafka_identity(
    model: dict[str, object], *, topic: str, consumer_group: str
) -> None:
    """Check that every producer, consumer, initializer, and monitor agrees."""

    services = model["services"]
    assert isinstance(services, dict)

    for service_name in ("listener", "scoring-worker"):
        service = services[service_name]
        assert isinstance(service, dict)
        environment = service["environment"]
        assert isinstance(environment, dict)
        assert environment["KAFKA_CLAIM_SUBMITTED_TOPIC"] == topic
        assert environment["KAFKA_CONSUMER_GROUP_ID"] == consumer_group

    kafka_init = services["kafka-init"]
    kafka_exporter = services["kafka-exporter"]
    assert isinstance(kafka_init, dict)
    assert isinstance(kafka_exporter, dict)
    assert f'--topic "{topic}"' in _command_text(kafka_init)
    assert f"--topic.filter={topic}" in _command_text(kafka_exporter)
    assert f"--group.filter={consumer_group}" in _command_text(kafka_exporter)


def test_default_kafka_identity_is_scoped_to_gasless_deployment() -> None:
    _assert_kafka_identity(
        _compose_model(),
        topic="claims.submitted.sepolia-gasless-v1",
        consumer_group="claims-registry-scorer-sepolia-gasless-v1",
    )


def test_custom_kafka_identity_reaches_init_apps_and_monitoring() -> None:
    _assert_kafka_identity(
        _compose_model(
            KAFKA_CLAIM_SUBMITTED_TOPIC="claims.submitted.test-isolated",
            KAFKA_CONSUMER_GROUP_ID="claims-registry-scorer-test-isolated",
        ),
        topic="claims.submitted.test-isolated",
        consumer_group="claims-registry-scorer-test-isolated",
    )


def test_scoring_dead_letter_has_an_owned_persistent_volume() -> None:
    """A read-only worker must be able to persist quarantine before committing."""

    model = _compose_model()
    services = model["services"]
    assert isinstance(services, dict)
    worker = services["scoring-worker"]
    state_init = services["scoring-state-init"]
    assert isinstance(worker, dict)
    assert isinstance(state_init, dict)

    environment = worker["environment"]
    assert isinstance(environment, dict)
    assert environment["SCORING_STATE_DIR"] == "/var/lib/claims-scoring"

    # The init container and worker must mount the same named volume. Otherwise
    # uid 10001 cannot write, quarantine fails closed, and the poison claim once
    # again remains the first uncommitted message in its partition.
    worker_volumes = worker["volumes"]
    init_volumes = state_init["volumes"]
    assert isinstance(worker_volumes, list)
    assert isinstance(init_volumes, list)
    assert "chown -R 10001:10001 /state" in _command_text(state_init)
    assert any(
        volume.get("source") == "claims-scoring-state"
        and volume.get("target") == "/var/lib/claims-scoring"
        for volume in worker_volumes
        if isinstance(volume, dict)
    )
    assert "test -w \"$SCORING_STATE_DIR\"" in VERIFY_SCRIPT.read_text(
        encoding="utf-8"
    )
    assert any(
        volume.get("source") == "claims-scoring-state"
        and volume.get("target") == "/state"
        for volume in init_volumes
        if isinstance(volume, dict)
    )


def test_gcp_verification_and_dashboard_do_not_select_legacy_identity() -> None:
    legacy_topic = "claims.submitted.v1"
    legacy_group = "claims-registry-scorer-v1"

    assert legacy_topic not in VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert legacy_group not in DASHBOARD.read_text(encoding="utf-8")


def _run_deploy_validation(
    env_file: Path, *, topic: str, consumer_group: str
) -> subprocess.CompletedProcess[str]:
    """Run only the deploy script's pre-Docker validation path."""

    env_file.write_text(
        "\n".join(
            (
                'CLAIMS_DEPLOYMENT_ID="sepolia-gasless-v2"',
                f'KAFKA_CLAIM_SUBMITTED_TOPIC="{topic}"',
                f'KAFKA_CONSUMER_GROUP_ID="{consumer_group}"',
                'XGBOOST_MODEL_HOST_DIR="missing-model"',
            )
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        ("bash", str(DEPLOY_SCRIPT), str(env_file)),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_deploy_rejects_topic_from_another_contract_deployment(tmp_path: Path) -> None:
    completed = _run_deploy_validation(
        tmp_path / ".env.gcp",
        topic="claims.submitted.sepolia-gasless-v1",
        consumer_group="claims-registry-scorer-sepolia-gasless-v2",
    )

    assert completed.returncode == 1
    assert "KAFKA_CLAIM_SUBMITTED_TOPIC must be scoped" in completed.stderr


def test_deploy_rejects_consumer_group_from_another_deployment(tmp_path: Path) -> None:
    completed = _run_deploy_validation(
        tmp_path / ".env.gcp",
        topic="claims.submitted.sepolia-gasless-v2",
        consumer_group="claims-registry-scorer-sepolia-gasless-v1",
    )

    assert completed.returncode == 1
    assert "KAFKA_CONSUMER_GROUP_ID must be scoped" in completed.stderr


def test_deploy_accepts_matching_deployment_scoped_identity(tmp_path: Path) -> None:
    completed = _run_deploy_validation(
        tmp_path / ".env.gcp",
        topic="claims.submitted.sepolia-gasless-v2",
        consumer_group="claims-registry-scorer-sepolia-gasless-v2",
    )

    # The Kafka validation passed. The later missing-model check intentionally
    # stops the script before it can build images or start containers.
    assert completed.returncode == 1
    assert "KAFKA_" not in completed.stderr
    assert "reviewed XGBoost artifact is missing" in completed.stderr
