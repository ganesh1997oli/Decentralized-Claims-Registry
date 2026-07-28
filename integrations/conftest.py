"""Disposable infrastructure fixtures shared by integration tests."""

import os
from uuid import uuid4

from confluent_kafka.admin import AdminClient, NewTopic
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from integrations.kafka import KafkaSettings
from integrations.postgres import PostgresAssessmentRepository


@pytest.fixture
def kafka_settings():
    """Provide one isolated Kafka topic and consumer group for each test.

    Integration tests must not share the application's development topic.  A
    unique topic prevents messages from another test run from satisfying an
    assertion accidentally, while a unique consumer group guarantees that the
    test begins with its own offsets.
    """

    bootstrap_servers = os.environ.get(
        "TEST_KAFKA_BOOTSTRAP_SERVERS",
        "",
    ).strip()
    if not bootstrap_servers:
        pytest.skip("set TEST_KAFKA_BOOTSTRAP_SERVERS to run Kafka integration tests")

    identity = uuid4().hex
    topic = f"claims.integration.{identity}"
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})

    # Topic auto-creation is deliberately disabled by the application.  Creating
    # the topic explicitly also lets this fixture wait for broker confirmation
    # before the listener attempts to publish.
    create_future = admin.create_topics(
        [NewTopic(topic, num_partitions=2, replication_factor=1)]
    )[topic]
    create_future.result(timeout=15)

    settings = KafkaSettings.from_mapping(
        {
            "KAFKA_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": bootstrap_servers,
            "KAFKA_CLAIM_SUBMITTED_TOPIC": topic,
            "KAFKA_CLIENT_ID": f"claims-integration-{identity}",
            "KAFKA_CONSUMER_GROUP_ID": f"claims-integration-{identity}",
            "KAFKA_DELIVERY_TIMEOUT_MS": "10000",
            "KAFKA_CONSUMER_POLL_SECONDS": "0.5",
        }
    )

    try:
        yield settings
    finally:
        # The generated topic is the only broker state owned by this fixture.
        # Deleting it keeps local and CI brokers clean without touching the
        # application's claims.submitted.v1 topic.
        delete_future = admin.delete_topics([topic], operation_timeout=10)[topic]
        delete_future.result(timeout=15)


@pytest.fixture
def postgres_repository():
    """Provide a repository isolated in a uniquely named PostgreSQL schema."""

    database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")

    schema_name = f"claims_test_{uuid4().hex}"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )

    def connect(url: str):
        return psycopg.connect(
            url,
            options=f"-c search_path={schema_name}",
            row_factory=dict_row,
        )

    repository = PostgresAssessmentRepository(database_url, connect=connect)
    try:
        repository.ensure_schema()
        yield repository
    finally:
        # The generated and quoted identifier constrains CASCADE to this test's
        # disposable schema; no application schema or data can be selected.
        with psycopg.connect(database_url) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
