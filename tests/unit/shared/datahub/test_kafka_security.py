"""Unit tests for the DataHub Kafka security rule engine.

``src/shared/datahub/kafka_security.py`` holds the seven numbered validation rules of
[spec/API.md §DataHub Kafka security] in one place, because two independent callers
must reach identical verdicts: the admin ``PATCH`` schema (``422 INVALID_PARAMETER``)
and the event consumer, which re-asserts them against the stored JSONB row before
building a client.

Both public functions are pure — no Kafka, no DB, no Kubernetes.

Spec traceability:
- spec/API.md §DataHub Kafka security — the seven-row rule table; "Every rule below is
  evaluated against the **effective tuple**"; rules 3 and 4 "reject rather than ignore
  or auto-correct"; the rules 6/7 privilege-boundary rationale — the MSK broker *shape*
  rather than the ``amazonaws.com`` suffix, because "an ``amazonaws.com`` subdomain is
  not necessarily a broker and can be attacker-provisioned" — and the anchored region
  derivation.
- spec/feature/BACKEND.md §Kafka connection — "The derivation **anchors to the end of
  the host**, so a suffix-extended lookalike does not match."
"""

from __future__ import annotations

import pytest

from src.shared.datahub.kafka_security import (
    KAFKA_SASL_MECHANISMS,
    KAFKA_SECURITY_PROTOCOLS,
    check_kafka_security,
    derive_msk_region,
    split_brokers,
)

_MSK_BROKERS = (
    "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098,"
    "b-2.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098"
)


def _check(**overrides) -> object | None:
    """Evaluate the rule engine over a PLAINTEXT baseline with *overrides* applied.

    The baseline is the spec's default: an unsecured connection with no mechanism,
    no credential, and no region — which the rules must accept.
    """
    tuple_ = {
        "security_protocol": "PLAINTEXT",
        "sasl_mechanism": "",
        "sasl_username": "",
        "aws_region": "",
        "brokers": "kafka:9092",
        "submitted_sasl_password": None,
    }
    tuple_.update(overrides)
    return check_kafka_security(**tuple_)


# ── Vocabulary ───────────────────────────────────────────────────────────────


def test_protocol_vocabulary_matches_spec() -> None:
    """The four accepted ``kafka_security_protocol`` values.

    spec: API.md §DataHub Kafka security — ``PLAINTEXT`` (default) | ``SSL`` |
    ``SASL_PLAINTEXT`` | ``SASL_SSL``.
    """
    assert KAFKA_SECURITY_PROTOCOLS == frozenset(
        {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}
    )


def test_mechanism_vocabulary_matches_spec() -> None:
    """The four accepted ``kafka_sasl_mechanism`` values.

    spec: API.md §DataHub Kafka security — ``PLAIN`` | ``SCRAM-SHA-256`` |
    ``SCRAM-SHA-512`` | ``AWS_MSK_IAM``.
    """
    assert KAFKA_SASL_MECHANISMS == frozenset(
        {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512", "AWS_MSK_IAM"}
    )


@pytest.mark.parametrize("protocol", ["plaintext", "SASL", "SSL_SASL", "NONE"])
def test_unknown_protocol_is_rejected(protocol: str) -> None:
    """A protocol outside the vocabulary is a violation naming the protocol field.

    spec: API.md §DataHub Kafka security — the field's value set is closed.
    """
    violation = _check(security_protocol=protocol)
    assert violation is not None, f"{protocol!r} is not one of the four accepted protocols"
    assert violation.field == "kafka_security_protocol"


@pytest.mark.parametrize(
    "mechanism",
    [
        # The obviously-wrong side: wrong case, and mechanisms from other stacks.
        "scram-sha-256",
        "GSSAPI",
        "IAM",
        # The plausibly-wrong side: real SASL/Kafka names one letter away from ours.
        # ``SCRAM-SHA-1`` is a genuine SCRAM mechanism that MSK does not offer, and
        # ``OAUTHBEARER`` is the wire mechanism AWS_MSK_IAM is translated into — both
        # would look reasonable to an operator and to a too-loose prefix check.
        "SCRAM-SHA-1",
        "OAUTHBEARER",
    ],
)
def test_unknown_mechanism_is_rejected(mechanism: str) -> None:
    """A mechanism outside the vocabulary is a violation naming the mechanism field.

    ``OAUTHBEARER`` is deliberately not accepted here: it is the wire mechanism the
    consumer derives from ``AWS_MSK_IAM``, not a value an operator stores.

    spec: API.md §DataHub Kafka security — the field's value set is closed;
    spec/feature/BACKEND.md §Kafka connection — "``AWS_MSK_IAM`` is not a librdkafka
    mechanism. AWS implements it as OAUTHBEARER".
    """
    violation = _check(security_protocol="SASL_SSL", sasl_mechanism=mechanism)
    assert violation is not None, f"{mechanism!r} is not one of the four accepted mechanisms"
    assert violation.field == "kafka_sasl_mechanism"


def test_absent_protocol_is_treated_as_plaintext() -> None:
    """An empty ``kafka_security_protocol`` defaults to PLAINTEXT and is accepted.

    This is what lets an existing DataHub row that predates the Kafka tuple keep working.

    spec: API.md §DataHub Kafka security — ``PLAINTEXT`` (default); "All of it is
    optional".
    """
    assert _check(security_protocol="") is None


def test_bare_plaintext_tuple_is_accepted() -> None:
    """The fully-unset tuple is valid — none of the fields is required.

    spec: API.md §DataHub Kafka security — "All of it is optional — the fields do not
    participate in ``is_configured``".
    """
    assert _check() is None


# ── Rule 1: mechanism belongs to the SASL protocols and only to them ─────────


@pytest.mark.parametrize("protocol", ["SASL_PLAINTEXT", "SASL_SSL"])
def test_rule1_rejects_missing_mechanism_under_a_sasl_protocol(protocol: str) -> None:
    """Rule 1 (reject): a SASL protocol without a mechanism.

    spec: API.md §DataHub Kafka security rule 1 — "``kafka_sasl_mechanism`` is required
    when ``kafka_security_protocol`` is ``SASL_PLAINTEXT`` or ``SASL_SSL``".
    """
    violation = _check(security_protocol=protocol, sasl_mechanism="")
    assert violation is not None
    assert violation.field == "kafka_sasl_mechanism"


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SSL"])
def test_rule1_rejects_mechanism_under_a_non_sasl_protocol(protocol: str) -> None:
    """Rule 1 (reject): a mechanism supplied with a non-SASL protocol.

    spec: API.md §DataHub Kafka security rule 1 — "and rejected when it is ``PLAINTEXT``
    or ``SSL``".
    """
    violation = _check(
        security_protocol=protocol, sasl_mechanism="SCRAM-SHA-512", sasl_username="svc"
    )
    assert violation is not None
    assert violation.field == "kafka_sasl_mechanism"


@pytest.mark.parametrize(
    ("protocol", "mechanism"),
    [
        ("SASL_PLAINTEXT", "PLAIN"),
        ("SASL_SSL", "SCRAM-SHA-256"),
        ("SASL_SSL", "SCRAM-SHA-512"),
    ],
)
def test_rule1_accepts_a_mechanism_under_a_sasl_protocol(protocol: str, mechanism: str) -> None:
    """Rule 1 (accept): a SASL protocol paired with a mechanism and a username.

    spec: API.md §DataHub Kafka security rule 1.
    """
    assert _check(security_protocol=protocol, sasl_mechanism=mechanism, sasl_username="svc") is None


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SSL"])
def test_rule1_accepts_a_non_sasl_protocol_with_no_mechanism(protocol: str) -> None:
    """Rule 1 (accept): a non-SASL protocol carrying no mechanism.

    ``SSL`` is TLS without SASL — a valid secured configuration with no credential.

    spec: API.md §DataHub Kafka security rule 1.
    """
    assert _check(security_protocol=protocol, sasl_mechanism="") is None


# ── Rule 2: credential mechanisms need a username ────────────────────────────


@pytest.mark.parametrize("mechanism", ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"])
def test_rule2_rejects_a_credential_mechanism_without_a_username(mechanism: str) -> None:
    """Rule 2 (reject): PLAIN / SCRAM-* with no ``kafka_sasl_username``.

    spec: API.md §DataHub Kafka security rule 2 — "``kafka_sasl_username`` is required
    for ``PLAIN``, ``SCRAM-SHA-256``, and ``SCRAM-SHA-512``".
    """
    violation = _check(
        security_protocol="SASL_SSL", sasl_mechanism=mechanism, sasl_username=""
    )
    assert violation is not None
    assert violation.field == "kafka_sasl_username"


@pytest.mark.parametrize("mechanism", ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"])
def test_rule2_accepts_a_credential_mechanism_with_a_username(mechanism: str) -> None:
    """Rule 2 (accept): the same mechanisms once a username is present.

    spec: API.md §DataHub Kafka security rule 2.
    """
    assert (
        _check(
            security_protocol="SASL_SSL", sasl_mechanism=mechanism, sasl_username="dataspoke"
        )
        is None
    )


# ── Rule 3: AWS_MSK_IAM rejects submitted credentials ────────────────────────


def test_rule3_rejects_a_username_under_msk_iam() -> None:
    """Rule 3 (reject): a username with ``AWS_MSK_IAM``.

    The rejection is the point — silently dropping the username "would leave an
    operator believing a credential is in force when none is".

    spec: API.md §DataHub Kafka security rule 3.
    """
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        sasl_username="svc",
        brokers=_MSK_BROKERS,
    )
    assert violation is not None
    assert violation.field == "kafka_sasl_username"


def test_rule3_rejects_a_submitted_password_under_msk_iam() -> None:
    """Rule 3 (reject): a submitted ``kafka_sasl_password`` with ``AWS_MSK_IAM``.

    spec: API.md §DataHub Kafka security rule 3 — "``kafka_sasl_username`` and
    ``kafka_sasl_password`` are **rejected** when ``kafka_sasl_mechanism`` is
    ``AWS_MSK_IAM``".
    """
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        brokers=_MSK_BROKERS,
        submitted_sasl_password="hunter2",
    )
    assert violation is not None
    assert violation.field == "kafka_sasl_password"


def test_rule3_is_evaluated_on_the_effective_username_not_the_body() -> None:
    """Rule 3 (reject, effective tuple): a *stored* username still blocks the switch.

    A PATCH that only sets ``kafka_sasl_mechanism=AWS_MSK_IAM`` carries no username,
    yet must be rejected while one remains stored — the operator clears it in the same
    request with an explicit ``""``.

    spec: API.md §DataHub Kafka security — "switching a working SCRAM configuration to
    ``AWS_MSK_IAM`` while a ``kafka_sasl_username`` is still stored is itself a
    rejected request — the operator clears it in the same ``PATCH`` with an explicit
    ``""``".
    """
    # The username here comes from the stored row, not from the request body; the
    # caller merges before calling, which is exactly what this asserts.
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        sasl_username="left-over-scram-user",
        brokers=_MSK_BROKERS,
        submitted_sasl_password=None,
    )
    assert violation is not None, (
        "a stored username must block the switch to AWS_MSK_IAM even when the body omits it"
    )
    assert violation.field == "kafka_sasl_username"

    # Backstop: clearing it in the same request makes the identical switch valid.
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            sasl_username="",
            brokers=_MSK_BROKERS,
        )
        is None
    )


def test_rule3_accepts_msk_iam_with_no_credentials() -> None:
    """Rule 3 (accept): ``AWS_MSK_IAM`` with neither username nor submitted password.

    spec: API.md §DataHub Kafka security rule 3; "``AWS_MSK_IAM`` is not a typable
    credential — it authenticates with the consumer pod's IAM identity".
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            brokers=_MSK_BROKERS,
            submitted_sasl_password=None,
        )
        is None
    )


def test_rule3_does_not_reject_an_empty_submitted_password() -> None:
    """An explicit ``""`` password is the clearing gesture, not a submitted credential.

    Rejecting it would make it impossible to clear a stored password in the same PATCH
    that selects ``AWS_MSK_IAM``.

    spec: API.md §Admin — "an empty-string secret clears it"; §DataHub Kafka security —
    rule 3 rejects a *submitted* credential.
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            brokers=_MSK_BROKERS,
            submitted_sasl_password="",
        )
        is None
    )


# ── Rule 4: AWS_MSK_IAM requires SASL_SSL ────────────────────────────────────


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SSL", "SASL_PLAINTEXT"])
def test_rule4_rejects_msk_iam_under_any_protocol_but_sasl_ssl(protocol: str) -> None:
    """Rule 4 (reject): ``AWS_MSK_IAM`` with a protocol other than ``SASL_SSL``.

    The verdict is a rejection, never a silent upgrade: "Silently upgrading the protocol
    to ``SASL_SSL`` would make the stored ``kafka_security_protocol`` a value the
    consumer does not actually use, so a ``GET`` would misreport the live connection."

    spec: API.md §DataHub Kafka security rule 4.
    """
    violation = _check(
        security_protocol=protocol, sasl_mechanism="AWS_MSK_IAM", brokers=_MSK_BROKERS
    )
    assert violation is not None, f"AWS_MSK_IAM must not be accepted with {protocol}"
    # Rule 1 owns the PLAINTEXT/SSL case (a mechanism with a non-SASL protocol); rule 4
    # owns SASL_PLAINTEXT. Either way the request is refused rather than corrected.
    assert violation.field in {"kafka_security_protocol", "kafka_sasl_mechanism"}


def test_rule4_names_the_protocol_field_for_sasl_plaintext() -> None:
    """Rule 4 (reject): ``SASL_PLAINTEXT`` + ``AWS_MSK_IAM`` blames the protocol.

    This is the combination rule 1 lets through, so rule 4 is what stops it — and it is
    the dangerous one: a SigV4 token minted from the pod's IAM identity on an
    unencrypted wire.

    spec: API.md §DataHub Kafka security rule 4 — "any other protocol is rejected".
    """
    violation = _check(
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="AWS_MSK_IAM",
        brokers=_MSK_BROKERS,
    )
    assert violation is not None
    assert violation.field == "kafka_security_protocol"


def test_rule4_accepts_msk_iam_with_sasl_ssl() -> None:
    """Rule 4 (accept): ``AWS_MSK_IAM`` over ``SASL_SSL``.

    spec: API.md §DataHub Kafka security rule 4.
    """
    assert (
        _check(
            security_protocol="SASL_SSL", sasl_mechanism="AWS_MSK_IAM", brokers=_MSK_BROKERS
        )
        is None
    )


# ── Rule 5: region only with AWS_MSK_IAM ─────────────────────────────────────


@pytest.mark.parametrize(
    ("protocol", "mechanism", "username"),
    [
        ("PLAINTEXT", "", ""),
        ("SSL", "", ""),
        ("SASL_SSL", "SCRAM-SHA-512", "svc"),
    ],
)
def test_rule5_rejects_a_region_without_msk_iam(
    protocol: str, mechanism: str, username: str
) -> None:
    """Rule 5 (reject): ``kafka_aws_region`` set under any non-IAM mechanism.

    spec: API.md §DataHub Kafka security rule 5 — "``kafka_aws_region`` is accepted only
    with ``AWS_MSK_IAM``".
    """
    violation = _check(
        security_protocol=protocol,
        sasl_mechanism=mechanism,
        sasl_username=username,
        aws_region="us-east-1",
    )
    assert violation is not None
    assert violation.field == "kafka_aws_region"


def test_rule5_accepts_a_region_with_msk_iam() -> None:
    """Rule 5 (accept): an explicit region alongside ``AWS_MSK_IAM``.

    The region must be the one ``_MSK_BROKERS`` encodes — rule 7 rejects a region that
    contradicts the hosts, so "accepted with AWS_MSK_IAM" is only observable when the two
    agree.

    spec: API.md §DataHub Kafka security rule 5; the region "exists only to sign an MSK
    IAM token"; rule 7 — "When ``kafka_aws_region`` is set and the broker hosts encode a
    region, the two must agree".
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            aws_region="us-east-1",
            brokers=_MSK_BROKERS,
        )
        is None
    )


def test_rule5_accepts_an_absent_region_with_msk_iam() -> None:
    """Rule 5 (accept): the region is optional — it falls back to hostname derivation.

    spec: API.md §DataHub Kafka security — ``kafka_aws_region`` is "Optional — falls
    back to derivation from the broker hostname".
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            aws_region="",
            brokers=_MSK_BROKERS,
        )
        is None
    )


# ── Rule 6: under AWS_MSK_IAM every broker host must have the MSK broker shape ──


@pytest.mark.parametrize(
    "brokers",
    [
        # -- The obviously-wrong side: hosts nowhere near AWS.
        pytest.param("kafka.evil.tld:9098", id="wholly-foreign-host"),
        pytest.param(
            f"{_MSK_BROKERS},kafka.evil.tld:9098", id="one-foreign-host-among-msk-hosts"
        ),
        pytest.param(
            "b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld:9098",
            id="SECURITY-suffix-extended-lookalike",
        ),
        pytest.param("amazonaws.com.evil.tld:9098", id="SECURITY-domain-as-prefix"),
        pytest.param("evil-amazonaws.com:9098", id="SECURITY-no-dot-before-amazonaws"),
        # -- The plausibly-wrong side: genuine AWS hosts that are not MSK brokers.
        # A suffix check on ``*.amazonaws.com`` admits all of these, which is exactly
        # the escalation rule 6 exists to close: an EC2 instance is routinely under a
        # tenant's own control and can carry a publicly-trusted certificate.
        pytest.param(
            "ec2-203-0-113-25.compute-1.amazonaws.com:9098",
            id="SECURITY-aws-host-that-is-not-an-msk-broker-ec2",
        ),
        pytest.param(
            "my-bucket.s3.amazonaws.com:9098",
            id="SECURITY-aws-host-that-is-not-an-msk-broker-s3",
        ),
        pytest.param(
            f"{_MSK_BROKERS},ec2-203-0-113-25.compute-1.amazonaws.com:9098",
            id="SECURITY-aws-non-broker-smuggled-among-msk-hosts",
        ),
        # -- The near-miss side: the MSK domain without the shape the regex requires.
        pytest.param("kafka.us-east-1.amazonaws.com:9098", id="msk-domain-with-no-broker-label"),
        pytest.param(
            "b-1.imazon.abc123.c2.kafka.amazonaws.com:9098", id="msk-shape-missing-the-region"
        ),
    ],
)
def test_rule6_rejects_a_host_without_the_msk_broker_shape(brokers: str) -> None:
    """Rule 6 (reject): any broker host that is not an MSK broker.

    This is a privilege boundary, not a typo check: "an Admin can point
    ``kafka_brokers`` at a host they control and the consumer will mint a SigV4-signed
    token from the pod's role and present it there, where it can be replayed against
    the real cluster".

    The rule matches the **MSK broker host shape**, not the ``amazonaws.com`` suffix.
    The suffix is not a boundary: "an ``amazonaws.com`` subdomain is not necessarily a
    broker and can be attacker-provisioned … an EC2 host is routinely under a tenant's
    own control with a publicly-trusted certificate obtainable for it. A suffix check
    therefore leaves the escalation intact, one step removed."  The shape is anchored at
    **both** ends, so neither a prefix nor a suffix extension passes either.

    spec: API.md §DataHub Kafka security rule 6 — "every host in it must have the MSK
    broker shape — a host under ``kafka.<region>.amazonaws.com`` or
    ``kafka-serverless.<region>.amazonaws.com``. Evaluated per host, not against the
    whole string" — and the rules 6/7 rationale paragraph.
    """
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        brokers=brokers,
    )
    assert violation is not None, f"{brokers!r} must not be accepted under AWS_MSK_IAM"
    assert violation.field == "kafka_brokers"


def test_rule6_rejects_an_empty_broker_list() -> None:
    """Rule 6 (reject): no brokers at all.

    "Every host has the MSK broker shape" is vacuously true of an empty list, so the
    empty case is rejected explicitly — otherwise the rule that guards the pod's IAM
    identity would be satisfiable by supplying nothing.

    spec: API.md §DataHub Kafka security rule 6 — "``kafka_brokers`` must be non-empty
    and **every** host in it must have the MSK broker shape".
    """
    violation = _check(
        security_protocol="SASL_SSL", sasl_mechanism="AWS_MSK_IAM", brokers=""
    )
    assert violation is not None, "AWS_MSK_IAM with no brokers must be rejected, not vacuously ok"
    assert violation.field == "kafka_brokers"


@pytest.mark.parametrize(
    "brokers",
    [
        pytest.param(_MSK_BROKERS, id="two-msk-hosts-with-ports"),
        pytest.param(
            "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com", id="single-host-no-port"
        ),
        pytest.param(
            "  b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098 , "
            "b-2.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098  ",
            id="whitespace-padded-entries",
        ),
        pytest.param(
            "boot-abc123.c1.kafka-serverless.us-east-1.amazonaws.com:9098",
            id="msk-serverless-endpoint",
        ),
    ],
)
def test_rule6_accepts_msk_broker_hosts(brokers: str) -> None:
    """Rule 6 (accept): every host in MSK broker shape, provisioned or serverless.

    The serverless case is the reason the shape check is not simply "contains
    ``.kafka.``" — ``kafka-serverless`` is an equally legitimate broker host.

    spec: API.md §DataHub Kafka security rule 6 — "a host under
    ``kafka.<region>.amazonaws.com`` or ``kafka-serverless.<region>.amazonaws.com``".
    """
    assert (
        _check(
            security_protocol="SASL_SSL", sasl_mechanism="AWS_MSK_IAM", brokers=brokers
        )
        is None
    )


def test_rule6_does_not_constrain_brokers_without_msk_iam() -> None:
    """Rule 6 applies only under ``AWS_MSK_IAM`` — SCRAM may point anywhere.

    spec: API.md §DataHub Kafka security rule 6 — "**Under ``AWS_MSK_IAM``**, every host
    …"; the rationale is that only IAM lends the pod's own identity to the broker.
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="SCRAM-SHA-512",
            sasl_username="svc",
            brokers="kafka.internal.corp:9093",
        )
        is None
    )


# ── Rule 7: an explicit region must agree with the one the hosts encode ─────


def test_SECURITY_rule7_rejects_a_region_contradicting_the_broker_hosts() -> None:
    """Rule 7 (reject): ``kafka_aws_region`` naming a region the hosts do not.

    This closes rule 6's matching edge on the other input.  The host shape pins *which
    cluster* is reachable and the region pins *which account's endpoint* the token is
    signed for; an operator who could supply a contradicting region would be steering
    the signature away from the cluster the hostname names.

    spec: API.md §DataHub Kafka security rule 7 — "When ``kafka_aws_region`` is set and
    the broker hosts encode a region, the two must agree"; the rationale — "an operator
    who supplies ``kafka_aws_region`` explicitly must not be able to use it to reach a
    host the region in the name contradicts".
    """
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        aws_region="eu-west-1",  # the hosts encode us-east-1
        brokers=_MSK_BROKERS,
    )
    assert violation is not None, (
        "an explicit region contradicting the broker hosts must be rejected"
    )
    assert violation.field == "kafka_aws_region", (
        "rule 7 blames the field the operator supplied, not the hosts"
    )

    # Backstop: the identical tuple with the agreeing region is accepted, so the
    # rejection above is the contradiction and not something else in the tuple.
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            aws_region="us-east-1",
            brokers=_MSK_BROKERS,
        )
        is None
    )


def test_SECURITY_rule7_rejects_a_mixed_region_broker_list() -> None:
    """Rule 7 (reject): hosts that disagree with each other, blamed on ``kafka_brokers``.

    Every host passes the rule-6 shape check here, so this is the case rule 7 adds: a
    list with no single encoded region.  It is rejected at validation time rather than
    surfacing later as an opaque connect-time failure.

    spec: API.md §DataHub Kafka security rule 7 — the two must agree, which presupposes
    the hosts name one region; rule 6 — "Evaluated per host, not against the whole
    string".
    """
    mixed = (
        "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098,"
        "b-2.imazon.abc123.c2.kafka.eu-west-1.amazonaws.com:9098"
    )
    violation = _check(
        security_protocol="SASL_SSL", sasl_mechanism="AWS_MSK_IAM", brokers=mixed
    )
    assert violation is not None, "a mixed-region broker list must be rejected"
    assert violation.field == "kafka_brokers", (
        "the disagreement is between the hosts, so the hosts are named"
    )


def test_SECURITY_rule7_rejects_a_mixed_region_list_even_when_a_region_is_supplied() -> None:
    """Rule 7 (reject): an explicit region does not rescue a self-contradicting host list.

    Naming ``us-east-1`` while one host lives in ``eu-west-1`` would otherwise let an
    operator satisfy "the region agrees" against whichever host happened to match.

    spec: API.md §DataHub Kafka security rule 7; rule 6 — every host, per host.
    """
    mixed = (
        "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098,"
        "b-2.imazon.abc123.c2.kafka.eu-west-1.amazonaws.com:9098"
    )
    violation = _check(
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
        aws_region="us-east-1",
        brokers=mixed,
    )
    assert violation is not None
    assert violation.field == "kafka_brokers"


def test_rule7_accepts_a_region_that_matches_the_broker_hosts() -> None:
    """Rule 7 (accept): the explicit region and the encoded region name the same place.

    spec: API.md §DataHub Kafka security rule 7.
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="AWS_MSK_IAM",
            aws_region="eu-central-1",
            brokers="boot-abc123.c1.kafka-serverless.eu-central-1.amazonaws.com:9098",
        )
        is None
    )


def test_rule7_does_not_apply_without_msk_iam() -> None:
    """Rule 7 is scoped to ``AWS_MSK_IAM``; rule 5 already bars a region elsewhere.

    The backstop that keeps rule 7 from being read as a general broker constraint: a
    SCRAM connection to an unrelated host is unaffected.

    spec: API.md §DataHub Kafka security rule 5 — the region "is accepted only with
    ``AWS_MSK_IAM``"; rule 7 is stated under the same mechanism.
    """
    assert (
        _check(
            security_protocol="SASL_SSL",
            sasl_mechanism="SCRAM-SHA-512",
            sasl_username="svc",
            brokers="b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
        )
        is None
    )


# ── split_brokers ────────────────────────────────────────────────────────────


def test_split_brokers_trims_and_drops_empties() -> None:
    """``bootstrap.servers`` is a comma-separated list; blanks are not hosts.

    spec: feature/BACKEND.md §Kafka connection — ``kafka_brokers`` maps to
    ``bootstrap.servers``.
    """
    assert split_brokers(" a:1 , ,b:2,") == ["a:1", "b:2"]
    assert split_brokers("") == []
    assert split_brokers("   ") == []


# ── derive_msk_region ────────────────────────────────────────────────────────


def test_derive_region_from_msk_hostnames() -> None:
    """The region is read off the MSK broker hostname when no explicit one is set.

    spec: feature/BACKEND.md §Kafka connection — "otherwise from the broker hostname,
    which for MSK encodes it (``…c2.kafka.<region>.amazonaws.com``)".
    """
    assert derive_msk_region(_MSK_BROKERS) == "us-east-1"


def test_derive_region_without_a_port() -> None:
    """A host with no ``:port`` suffix derives the same region.

    spec: feature/BACKEND.md §Kafka connection — derivation is on the host.
    """
    assert derive_msk_region("b-1.imazon.abc.c2.kafka.ap-northeast-2.amazonaws.com") == (
        "ap-northeast-2"
    )


def test_derive_region_for_msk_serverless() -> None:
    """MSK Serverless endpoints encode the region the same way.

    spec: feature/BACKEND.md §Kafka connection — the MSK hostname encodes the region.
    """
    assert derive_msk_region("boot-abc.c1.kafka-serverless.eu-central-1.amazonaws.com:9098") == (
        "eu-central-1"
    )


def test_derive_region_returns_none_for_a_non_msk_host() -> None:
    """A host that encodes no region yields ``None`` so the caller fails loudly.

    spec: feature/BACKEND.md §Kafka connection — "When neither source resolves, the
    consumer fails loudly and reports the reason rather than guessing a region".
    """
    assert derive_msk_region("kafka.internal.corp:9093") is None
    assert derive_msk_region("") is None


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("ec2-203-0-113-25.compute-1.amazonaws.com:9098", id="ec2-instance"),
        pytest.param("my-bucket.s3.amazonaws.com:9098", id="s3-bucket"),
        pytest.param("abc123.execute-api.us-east-1.amazonaws.com:9098", id="api-gateway"),
    ],
)
def test_SECURITY_derive_region_returns_none_for_an_aws_host_that_is_not_a_broker(
    host: str,
) -> None:
    """Regression: an ``amazonaws.com`` host that is not an MSK broker derives nothing.

    The same expression is rule 6's shape check and the region extractor, so this is the
    derivation-side statement of the escalation rule 6 closes: an EC2 or S3 host carries
    the ``amazonaws.com`` suffix but is not a broker, and an API Gateway host even embeds
    a region — none of them may yield one here, or a suffix-shaped check would let the
    pod's SigV4 token be signed for a host the operator controls.

    spec: API.md §DataHub Kafka security — "an ``amazonaws.com`` subdomain is not
    necessarily a broker and can be attacker-provisioned:
    ``ec2-203-0-113-25.compute-1.amazonaws.com`` and ``my-bucket.s3.amazonaws.com`` both
    carry the suffix … A suffix check therefore leaves the escalation intact".
    """
    assert derive_msk_region(host) is None, (
        f"{host!r} is an AWS host but not an MSK broker; no region may be derived from it"
    )


def test_SECURITY_derive_region_rejects_a_suffix_extended_lookalike() -> None:
    """Regression: the derivation is anchored at the END of the host.

    An unanchored match would read ``b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld``
    as ``us-east-1`` and sign a token for a host the attacker owns.  This encodes a
    fixed vulnerability — the assertion must stay.

    spec: API.md §DataHub Kafka security — "region derivation from the broker hostname
    **anchors to the end of the host** — an unanchored match accepts
    ``b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld`` as ``us-east-1``".
    """
    assert derive_msk_region("b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld") is None
    assert derive_msk_region("b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld:9098") is None


def test_SECURITY_derive_region_is_applied_per_host_not_to_the_whole_string() -> None:
    """Regression: every entry must match; one foreign host poisons the derivation.

    A search over the joined ``bootstrap.servers`` string would find ``eu-west-1`` in
    ``evil.tld:9098,b-1.c.x.c2.kafka.eu-west-1.amazonaws.com:9098`` and sign for it
    while the client also dials ``evil.tld``.  This encodes a fixed vulnerability.

    spec: API.md §DataHub Kafka security — the anchoring rationale; rule 6 requires
    *every* host to be an AWS host.
    """
    assert (
        derive_msk_region("evil.tld:9098,b-1.c.x.c2.kafka.eu-west-1.amazonaws.com:9098") is None
    )


def test_SECURITY_derive_region_refuses_a_mixed_region_broker_list() -> None:
    """Regression: disagreeing regions yield ``None``, not an arbitrary pick.

    Signing for whichever region happened to sort first would authenticate against a
    cluster the operator did not name.

    spec: API.md §DataHub Kafka security — the region exists only to sign an MSK IAM
    token; feature/BACKEND.md §Kafka connection — the consumer "fails loudly" rather
    than guessing.
    """
    mixed = (
        "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098,"
        "b-2.imazon.abc.c2.kafka.eu-west-1.amazonaws.com:9098"
    )
    assert derive_msk_region(mixed) is None

    # Backstop: the same two hosts in ONE region do derive, so the None above is the
    # disagreement and not a parsing failure.
    same = (
        "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098,"
        "b-2.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098"
    )
    assert derive_msk_region(same) == "us-east-1"
