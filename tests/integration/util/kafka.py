"""Kafka dummy-data reset utilities for integration tests.

Uses confluent_kafka to delete/recreate Imazon topics and produce seed messages
from JSONL fixture files under fixtures/kafka/.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_TOPICS: dict[str, str] = {
    "imazon.orders.events": "orders.jsonl",
    "imazon.shipping.updates": "shipping.jsonl",
    "imazon.reviews.new": "reviews.jsonl",
}

_FIXTURES_DIR: Path = Path(__file__).parent / "fixtures" / "kafka"

# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load dev_env/.env into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "dev_env" / ".env"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_kafka_bootstrap = os.environ.get(
    "DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS", "localhost:9104"
)

# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------


def _get_admin_client() -> AdminClient:
    """Create a Kafka AdminClient."""
    return AdminClient({"bootstrap.servers": _kafka_bootstrap})


def _get_producer() -> Producer:
    """Create a Kafka Producer."""
    return Producer({"bootstrap.servers": _kafka_bootstrap})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _delete_topics(admin: AdminClient, topics: list[str]) -> None:
    """Delete the given topics and wait for each future to resolve."""
    futures = admin.delete_topics(topics, operation_timeout=10)
    for topic, future in futures.items():
        try:
            future.result()
        except Exception as exc:
            # Topic may not exist yet — treat as non-fatal.
            print(f"  [WARN] delete_topics({topic}): {exc}")


def _create_topics(admin: AdminClient, topics: list[str]) -> None:
    """Create the given topics with 1 partition and replication_factor=1."""
    new_topics = [NewTopic(t, num_partitions=1, replication_factor=1) for t in topics]
    futures = admin.create_topics(new_topics, operation_timeout=10)
    for topic, future in futures.items():
        try:
            future.result()
        except Exception as exc:
            print(f"  [WARN] create_topics({topic}): {exc}")


def _produce_messages(producer: Producer, topic: str, jsonl_file: str) -> int:
    """Read a JSONL fixture file and produce each line as a Kafka message.

    Returns the number of messages produced.
    """
    fixture_path = _FIXTURES_DIR / jsonl_file
    count = 0
    for line in fixture_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        producer.produce(topic, value=line.encode("utf-8"))
        count += 1
    producer.flush()
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reset_all() -> None:
    """Delete and recreate all 3 Imazon topics, then produce all seed messages."""
    reset_topics(set(ALL_TOPICS.keys()))


def reset_topics(topics: set[str]) -> None:
    """Delete and recreate specific topics, then produce their seed messages.

    A 2-second sleep is inserted between delete and create to allow the broker
    to propagate the deletion before recreation.
    """
    topic_list = [t for t in topics if t in ALL_TOPICS]
    if not topic_list:
        return

    admin = _get_admin_client()
    producer = _get_producer()

    _delete_topics(admin, topic_list)
    time.sleep(2)
    _create_topics(admin, topic_list)

    for topic in topic_list:
        jsonl_file = ALL_TOPICS[topic]
        count = _produce_messages(producer, topic, jsonl_file)
        print(f"  Produced {count} messages to {topic}.")


def load_seed_messages(topic: str) -> list[dict]:  # type: ignore[type-arg]
    """Load and parse JSONL seed messages for a topic.

    Returns a list of parsed dicts, useful for test assertions without
    consuming from Kafka.
    """
    jsonl_file = ALL_TOPICS.get(topic)
    if jsonl_file is None:
        raise ValueError(f"Unknown topic: {topic!r}. Known topics: {list(ALL_TOPICS)}")
    fixture_path = _FIXTURES_DIR / jsonl_file
    messages = []
    for line in fixture_path.read_text().splitlines():
        line = line.strip()
        if line:
            messages.append(json.loads(line))
    return messages
