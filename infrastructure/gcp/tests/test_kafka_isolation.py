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


def _compose_model(
    *, include_training: bool = False, **overrides: str
) -> dict[str, object]:
    """Resolve Compose exactly as deployment would, with optional test values."""

    environment = os.environ.copy()
    environment.update(overrides)
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_EXAMPLE),
        "--file",
        str(COMPOSE_FILE),
    ]
    if include_training:
        command.extend(("--profile", "training"))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
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


def test_default_kafka_identity_is_scoped_to_public_intake_deployment() -> None:
    _assert_kafka_identity(
        _compose_model(),
        topic="claims.submitted.sepolia-public-intake-v1",
        consumer_group="claims-registry-scorer-sepolia-public-intake-v1",
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


def test_public_intake_configuration_reaches_the_api() -> None:
    model = _compose_model()
    services = model["services"]
    assert isinstance(services, dict)
    backend = services["backend"]
    assert isinstance(backend, dict)
    environment = backend["environment"]
    assert isinstance(environment, dict)

    required = {
        "CLAIMANT_AUTH_DOMAIN",
        "CLAIMANT_AUTH_URI",
        "CLAIMANT_SESSION_SIGNING_KEY",
        "CLAIMANT_SUBJECT_KEY",
        "CLAIMANT_AUTH_FINGERPRINT_KEY",
        "POLICY_REFERENCE_LOOKUP_KEY",
        "CLAIMANT_COMMITMENT_KEY",
        "POLICY_ELIGIBILITY_RECORDS_JSON",
        "CLAIM_PERMIT_ISSUERS_JSON",
    }
    assert required <= environment.keys()
    assert environment["CLAIMS_DEPLOYMENT_ID"] == "sepolia-public-intake-v1"

    volumes = backend["volumes"]
    assert isinstance(volumes, list)
    assert any(
        volume.get("target") == "/run/secrets/permit-issuers"
        and volume.get("read_only") is True
        for volume in volumes
        if isinstance(volume, dict)
    )


def test_public_demo_switch_defaults_secure_and_reaches_api_and_frontend() -> None:
    """One explicit switch must keep the browser and server mode aligned."""

    for configured, expected in ((None, "false"), ("true", "true")):
        model = (
            _compose_model()
            if configured is None
            else _compose_model(PUBLIC_DEMO_READ_ONLY=configured)
        )
        services = model["services"]
        assert isinstance(services, dict)
        backend = services["backend"]
        frontend = services["frontend"]
        assert isinstance(backend, dict)
        assert isinstance(frontend, dict)
        backend_environment = backend["environment"]
        frontend_build = frontend["build"]
        assert isinstance(backend_environment, dict)
        assert isinstance(frontend_build, dict)
        frontend_args = frontend_build["args"]
        assert isinstance(frontend_args, dict)

        assert backend_environment["PUBLIC_DEMO_READ_ONLY"] == expected
        assert frontend_args["VITE_PUBLIC_DEMO_READ_ONLY"] == expected


def test_public_writers_receive_only_mounted_private_keys() -> None:
    model = _compose_model()
    services = model["services"]
    assert isinstance(services, dict)

    expected = {
        "gasless-relayer": (
            "SEPOLIA_RELAYER_PRIVATE_KEY",
            "SEPOLIA_RELAYER_PRIVATE_KEY_FILE",
            "/run/secrets/relayer.key",
        ),
        "scoring-worker": (
            "SEPOLIA_ASSESSOR_PRIVATE_KEY",
            "SEPOLIA_ASSESSOR_PRIVATE_KEY_FILE",
            "/run/secrets/assessor.key",
        ),
    }
    for service_name, (raw_name, file_name, target) in expected.items():
        service = services[service_name]
        assert isinstance(service, dict)
        environment = service["environment"]
        assert isinstance(environment, dict)
        assert raw_name not in environment
        assert environment[file_name] == target
        assert environment["DEPLOYMENT_ENVIRONMENT"] == "production"
        volumes = service["volumes"]
        assert isinstance(volumes, list)
        assert any(
            volume.get("target") == target and volume.get("read_only") is True
            for volume in volumes
            if isinstance(volume, dict)
        )


def test_public_frontend_exposes_http_and_https_with_persistent_certificates() -> None:
    model = _compose_model()
    services = model["services"]
    assert isinstance(services, dict)
    frontend = services["frontend"]
    assert isinstance(frontend, dict)

    ports = frontend["ports"]
    assert isinstance(ports, list)
    assert {port["target"] for port in ports if isinstance(port, dict)} == {80, 443}
    volumes = frontend["volumes"]
    assert isinstance(volumes, list)
    assert {
        volume["target"] for volume in volumes if isinstance(volume, dict)
    } >= {"/data", "/config"}


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


def test_model_trainer_downloads_dataset_into_writable_tmpfs() -> None:
    """A read-only trainer must not download the dataset under /app."""

    model = _compose_model(include_training=True)
    services = model["services"]
    assert isinstance(services, dict)
    trainer = services["model-trainer"]
    assert isinstance(trainer, dict)

    assert trainer["read_only"] is True
    assert "/tmp:size=256m,mode=1777" in trainer["tmpfs"]
    command = _command_text(trainer)
    assert "--dataset /tmp/african_motor_claims.csv" in command


def test_gcp_verification_and_dashboard_do_not_select_legacy_identity() -> None:
    legacy_topic = "claims.submitted.v1"
    legacy_group = "claims-registry-scorer-v1"

    assert legacy_topic not in VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert legacy_group not in DASHBOARD.read_text(encoding="utf-8")


def test_deployment_waits_for_consumer_health_before_verification() -> None:
    """A release must not verify workers while they are still starting."""

    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "--wait" in deploy_script
    assert "--wait-timeout 240" in deploy_script

    model = _compose_model()
    services = model["services"]
    assert isinstance(services, dict)

    for service_name, metrics_port in (
        ("listener", 9101),
        ("scoring-worker", 9102),
    ):
        service = services[service_name]
        assert isinstance(service, dict)
        healthcheck = service.get("healthcheck")
        assert isinstance(healthcheck, dict)
        command = healthcheck.get("test")
        assert isinstance(command, list)
        assert f"http://127.0.0.1:{metrics_port}/metrics" in " ".join(command)


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

    # The Kafka validation passed. The later missing-model check
    # stops the script before it can build images or start containers.
    assert completed.returncode == 1
    assert "KAFKA_" not in completed.stderr
    assert "reviewed XGBoost artifact is missing" in completed.stderr
