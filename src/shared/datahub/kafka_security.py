"""Kafka security vocabulary and rule engine for the DataHub peripheral.

The rules of [spec/API.md §DataHub Kafka security](../../../spec/API.md) live here,
in one function, because two independent callers must reach identical verdicts:

- ``src/api/schemas/admin.py`` rejects an invalid ``PATCH`` with
  ``422 INVALID_PARAMETER``;
- ``src/shared/datahub/consumer.py`` re-asserts the same rules against the stored
  row before it builds a client, since ``peripheral_config.settings`` is untyped
  JSONB that direct SQL or a future writer can populate without passing through
  the admin schema.  The same convention guards
  ``src/api/routers/spoke/common/peripheral_links.py``.

Rule 6 in particular is a privilege boundary, not a typo check: under
``AWS_MSK_IAM`` the consumer signs a token with the pod's IAM identity, so a
broker host outside ``*.amazonaws.com`` — or a region derived from an unanchored
suffix match — hands that token to whoever owns the host.

Public surface:
    check_kafka_security(...) -> KafkaSecurityViolation | None
    derive_msk_region(brokers) -> str | None
    split_brokers(brokers) -> list[str]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KAFKA_SECURITY_PROTOCOLS: frozenset[str] = frozenset(
    {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}
)
KAFKA_SASL_MECHANISMS: frozenset[str] = frozenset(
    {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512", "AWS_MSK_IAM"}
)
# Mechanisms authenticated with a typed username/password pair, as opposed to
# AWS_MSK_IAM which authenticates with the consumer pod's IAM identity.
KAFKA_CREDENTIAL_MECHANISMS: frozenset[str] = frozenset(
    {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"}
)
KAFKA_SASL_PROTOCOLS: frozenset[str] = frozenset({"SASL_PLAINTEXT", "SASL_SSL"})

# MSK broker hostnames encode the region:
# b-1.<cluster>.<id>.c2.kafka.<region>.amazonaws.com  (provisioned)
# boot-<id>.c2.kafka-serverless.<region>.amazonaws.com  (serverless)
#
# This one pattern serves as both the rule-6 shape check and the region
# extractor. Anchored at both ends: an unanchored search would read
# ``b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld`` as ``us-east-1``, and a
# merely-``*.amazonaws.com`` check would admit any AWS-hosted service — an
# attacker-controlled EC2 host (``ec2-203-0-113-25.compute-1.amazonaws.com``) is
# an AWS host but is not an MSK broker, and would receive the SigV4 token.
_MSK_BROKER_HOST_RE = re.compile(
    r"^[A-Za-z0-9.-]+\.kafka(?:-serverless)?\.([a-z0-9-]+)\.amazonaws\.com$"
)


@dataclass(frozen=True)
class KafkaSecurityViolation:
    """A broken rule: the offending field and an operator-facing explanation."""

    field: str
    message: str


def split_brokers(brokers: str) -> list[str]:
    """Split a librdkafka ``bootstrap.servers`` string into its host[:port] entries."""
    return [entry.strip() for entry in brokers.split(",") if entry.strip()]


def _strip_port(host: str) -> str:
    return host.rsplit(":", 1)[0] if ":" in host else host


def derive_msk_region(brokers: str) -> str | None:
    """Return the AWS region encoded in the MSK broker hostnames, or None.

    Every host must agree; a mixed list is treated as underivable so the caller
    fails loudly rather than signing for whichever region happened to sort first.
    """
    regions = set()
    for entry in split_brokers(brokers):
        match = _MSK_BROKER_HOST_RE.match(_strip_port(entry))
        if match is None:
            return None
        regions.add(match.group(1))
    if len(regions) != 1:
        return None
    return regions.pop()


def check_kafka_security(
    *,
    security_protocol: str,
    sasl_mechanism: str,
    sasl_username: str,
    aws_region: str,
    brokers: str,
    submitted_sasl_password: str | None = None,
) -> KafkaSecurityViolation | None:
    """Evaluate the six Kafka security rules; return the first violation or None.

    All arguments describe the **effective** tuple — the stored settings with any
    pending patch merged over them — so a partial update is judged by the
    configuration it produces rather than by the fields it happens to carry.

    ``submitted_sasl_password`` is the request body's value (``None`` when the
    field is absent).  Rule 3 rejects a *submitted* credential under
    ``AWS_MSK_IAM``; a *stored* one is cleared by the caller rather than
    rejected, since it is state that has lost its purpose rather than an
    assertion the operator is making.
    """
    protocol = security_protocol or "PLAINTEXT"
    if protocol not in KAFKA_SECURITY_PROTOCOLS:
        return KafkaSecurityViolation(
            "kafka_security_protocol",
            "kafka_security_protocol must be one of "
            f"{', '.join(sorted(KAFKA_SECURITY_PROTOCOLS))}",
        )
    if sasl_mechanism and sasl_mechanism not in KAFKA_SASL_MECHANISMS:
        return KafkaSecurityViolation(
            "kafka_sasl_mechanism",
            f"kafka_sasl_mechanism must be one of {', '.join(sorted(KAFKA_SASL_MECHANISMS))}",
        )

    # Rule 1 — the mechanism belongs to the SASL protocols and only to them.
    if protocol in KAFKA_SASL_PROTOCOLS and not sasl_mechanism:
        return KafkaSecurityViolation(
            "kafka_sasl_mechanism",
            f"kafka_sasl_mechanism is required when kafka_security_protocol is {protocol}",
        )
    if protocol not in KAFKA_SASL_PROTOCOLS and sasl_mechanism:
        return KafkaSecurityViolation(
            "kafka_sasl_mechanism",
            f"kafka_sasl_mechanism is not allowed when kafka_security_protocol is {protocol}",
        )

    # Rule 2 — credential mechanisms need a user to authenticate as.
    if sasl_mechanism in KAFKA_CREDENTIAL_MECHANISMS and not sasl_username:
        return KafkaSecurityViolation(
            "kafka_sasl_username",
            f"kafka_sasl_username is required for kafka_sasl_mechanism {sasl_mechanism}",
        )

    if sasl_mechanism == "AWS_MSK_IAM":
        # Rule 3 — AWS_MSK_IAM authenticates with the pod's IAM identity, so a
        # supplied credential is rejected rather than dropped: silently ignoring
        # it would leave the operator believing it is in force.
        if sasl_username:
            return KafkaSecurityViolation(
                "kafka_sasl_username",
                "kafka_sasl_username is not accepted with kafka_sasl_mechanism AWS_MSK_IAM; "
                "clear it by sending an empty string",
            )
        if submitted_sasl_password:
            return KafkaSecurityViolation(
                "kafka_sasl_password",
                "kafka_sasl_password is not accepted with kafka_sasl_mechanism AWS_MSK_IAM",
            )
        # Rule 4 — reject rather than upgrade the protocol, so the stored value is
        # always the one the consumer actually uses and GET never misreports it.
        if protocol != "SASL_SSL":
            return KafkaSecurityViolation(
                "kafka_security_protocol",
                "kafka_sasl_mechanism AWS_MSK_IAM requires kafka_security_protocol SASL_SSL, "
                f"not {protocol}",
            )
        # Rule 6 — the pod's IAM identity is a deploy-time grant that an Admin
        # must not be able to redirect. A host outside the MSK broker shape would
        # receive a SigV4 token minted from that identity and could replay it.
        # The check is the MSK broker shape, not merely "an AWS host": an
        # attacker-controlled EC2 instance answers to *.amazonaws.com too.
        hosts = split_brokers(brokers)
        offending = [
            entry for entry in hosts if not _MSK_BROKER_HOST_RE.match(_strip_port(entry))
        ]
        if offending or not hosts:
            return KafkaSecurityViolation(
                "kafka_brokers",
                "kafka_sasl_mechanism AWS_MSK_IAM requires every kafka_brokers host to be an "
                "MSK broker of the form <broker>.kafka[-serverless].<region>.amazonaws.com; "
                f"rejected: {', '.join(offending) or '(none supplied)'}",
            )

        host_region = derive_msk_region(brokers)
        if host_region is None:
            return KafkaSecurityViolation(
                "kafka_brokers",
                "every kafka_brokers host must encode the same AWS region",
            )
        # An explicit region would otherwise be the weak edge of rule 6: the host
        # allowlist pins *which cluster* is reachable, and the region pins *which
        # account's endpoint* the token is signed for. They must describe the same
        # place.
        if aws_region and aws_region != host_region:
            return KafkaSecurityViolation(
                "kafka_aws_region",
                f"kafka_aws_region '{aws_region}' contradicts the region encoded in "
                f"kafka_brokers ('{host_region}')",
            )

    # Rule 5 — the region exists only to sign an MSK IAM token.
    if aws_region and sasl_mechanism != "AWS_MSK_IAM":
        return KafkaSecurityViolation(
            "kafka_aws_region",
            "kafka_aws_region is accepted only with kafka_sasl_mechanism AWS_MSK_IAM",
        )

    return None
