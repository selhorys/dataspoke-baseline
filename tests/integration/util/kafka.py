"""Kafka dummy-data reset utilities for integration tests.

Uses confluent_kafka to delete/recreate Imazon topics and produce seed messages
from JSONL fixture files under fixtures/kafka/.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from confluent_kafka import KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_TOPICS: dict[str, str] = {
    "imazon.orders.events": "imazon.orders.events.jsonl",
    "imazon.shipping.updates": "imazon.shipping.updates.jsonl",
}

_FIXTURES_DIR: Path = Path(__file__).parent / "fixtures" / "kafka"

# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load helm-charts/.env.dev into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env.dev"
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
    "DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS", "localhost:9104"
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
    """Delete the given topics and wait for each future to resolve.

    Fail-loud: raises on any delete failure so a reset never proceeds against a
    dirty baseline. Deleting a topic that does not exist is the one benign case
    (the deletion goal is already satisfied) and is tolerated.
    """
    futures = admin.delete_topics(topics, operation_timeout=10)
    for topic, future in futures.items():
        try:
            future.result()
        except KafkaException as exc:
            err = exc.args[0]
            if isinstance(err, KafkaError) and err.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                continue  # Absent topic — deletion already satisfied.
            raise RuntimeError(f"Kafka reset: delete_topics({topic}) failed: {exc}") from exc


def _wait_topics_absent(
    admin: AdminClient,
    topics: list[str],
    timeout: float = 20.0,
    interval: float = 0.5,
) -> None:
    """Block until none of ``topics`` are present in cluster metadata.

    Replaces a fixed post-delete sleep: the broker propagates a topic deletion
    asynchronously, so recreating immediately can hit TOPIC_ALREADY_EXISTS (which
    ``_create_topics`` treats as a dirty-baseline failure). Polling metadata exits
    as soon as the deletion has propagated (normally well under a second) instead
    of always paying a worst-case fixed wait.

    ``timeout`` is only an upper bound before declaring a genuine broker failure —
    on timeout this raises so a stuck deletion is loud, not silently reseeded onto
    stale data.
    """
    deadline = time.time() + timeout
    remaining = set(topics)
    while True:
        present = remaining & set(admin.list_topics(timeout=10).topics.keys())
        if not present:
            return
        if time.time() >= deadline:
            raise RuntimeError(
                f"Kafka reset: topics {sorted(present)} still present {timeout:.0f}s after "
                "delete_topics — broker never propagated the deletion. Recreating now would "
                "append seed messages onto stale data (a dirty baseline)."
            )
        time.sleep(interval)


def _create_topics(admin: AdminClient, topics: list[str]) -> None:
    """Create the given topics with 1 partition and replication_factor=1.

    Fail-loud: raises on any create failure. TOPIC_ALREADY_EXISTS is a failure
    here — it means the prior delete did not clear the topic, so producing seed
    messages would append to stale data (a dirty baseline).
    """
    new_topics = [NewTopic(t, num_partitions=1, replication_factor=1) for t in topics]
    futures = admin.create_topics(new_topics, operation_timeout=10)
    for topic, future in futures.items():
        try:
            future.result()
        except Exception as exc:
            raise RuntimeError(f"Kafka reset: create_topics({topic}) failed: {exc}") from exc


def _produce_messages(producer: Producer, topic: str, jsonl_file: str) -> int:
    """Read a JSONL fixture file and produce each line as a Kafka message.

    Wraps produce+flush in a bounded retry (up to 3 attempts, 2s/4s backoff) so a
    transient broker connect/flush timeout self-heals instead of burning the whole
    reset-seed. Topics are delete+recreate each reset, so re-producing is idempotent
    for our purposes. Returns the number of messages produced.
    """
    fixture_path = _FIXTURES_DIR / jsonl_file
    lines = [
        line.strip()
        for line in fixture_path.read_text().splitlines()
        if line.strip()
    ]

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            count = 0
            for line in lines:
                producer.produce(topic, value=line.encode("utf-8"))
                count += 1
            producer.flush()
            return count
        except Exception as exc:  # confluent_kafka raises on a broker timeout
            last_exc = exc
            if attempt < 2:
                backoff = 2 * (attempt + 1)
                print(
                    f"  [WARN] produce({topic}) attempt {attempt + 1} failed: "
                    f"{exc}; retrying in {backoff}s"
                )
                time.sleep(backoff)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reset_all_empty() -> None:
    """Delete and recreate all Imazon topics. No seed messages produced.

    Post-condition: topics exist but are empty.
    """
    topic_list = list(ALL_TOPICS.keys())
    admin = _get_admin_client()
    _delete_topics(admin, topic_list)
    _wait_topics_absent(admin, topic_list)
    _create_topics(admin, topic_list)


def reset_all() -> None:
    """Delete and recreate all Imazon topics, then produce all seed messages."""
    reset_topics(set(ALL_TOPICS.keys()))


def reset_topics(topics: set[str]) -> None:
    """Delete and recreate specific topics, then produce their seed messages.

    Between delete and create the broker's deletion is confirmed by polling
    cluster metadata (``_wait_topics_absent``) rather than a fixed sleep, so
    recreation starts as soon as the deletion has propagated.
    """
    topic_list = [t for t in topics if t in ALL_TOPICS]
    if not topic_list:
        return

    admin = _get_admin_client()
    producer = _get_producer()

    _delete_topics(admin, topic_list)
    _wait_topics_absent(admin, topic_list)
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
