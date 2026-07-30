"""Unit tests for ``src/shared/redaction.py`` — the operator-facing error scrubber.

``sanitize_error_message`` is the single control standing between a transport
exception's ``str()`` and three sinks with three reader populations: the
``peripheral_health.last_error`` column an Admin reads back over the API, the
internal activity's ``500`` body Airflow keeps in its task logs, and the API's own
logs.

Two obligations pull against each other, and both are pinned here as regression
cases because a refactor can silently reopen either:

1. **Credentials must not survive.** Every case below is a shape a real transport
   message carries.
2. **Diagnostics must survive verbatim.** These messages are the only operator
   signal for the subsystems that report them, so an over-broad pattern that eats
   the URN out of ``Unable to fetch entity with key: urn:…`` costs more than it
   protects.

Spec: spec/feature/BACKEND.md §Health reporting — "``last_error`` is bounded and
    credential-free. This binds every reporter writing the table … no credentials, no
    stack traces, and a length bound, so a persisted message cannot become a
    disclosure or log-forging surface."
Spec: spec/feature/BACKEND.md §Sync + mapping sweep step 2 §Trust boundary on
    writer-supplied patterns — untrusted text is "bounded in length and escaped before
    it reaches a log record: a writer cannot forge log structure or grow a record
    without limit." The same property is what the control-character strip below
    provides for transport text.
"""

import time

import pytest

from src.shared.redaction import (
    _MAX_SCAN_LENGTH,
    REDACTED,
    sanitize_error_message,
)

# ── Credential shapes that must be redacted ──────────────────────────────────
#
# Each case is (label, message, secret_substring_that_must_not_survive,
# name_fragment_that_must_survive). The surviving name fragment is asserted
# because the module's contract keeps the credential *name* and replaces only the
# value: a name is not a secret, and keeping it is what makes `?access_token=`
# distinguishable from `?token=` in an operator's read of the row.

_CREDENTIAL_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "query-string access_token",
        "GET /openapi/v3/entity?access_token=eyJhbGciOiJIUzI1NiJ9 failed with 500",
        "eyJhbGciOiJIUzI1NiJ9",
        "access_token",
    ),
    (
        "oauth client_secret",
        'token exchange rejected: {"client_secret": "s3cr3t-cl13nt-value"}',
        "s3cr3t-cl13nt-value",
        "client_secret",
    ),
    (
        "abbreviated pwd",
        "connection string rejected: host=pg port=5432 pwd=hunter2hunter2",
        "hunter2hunter2",
        "pwd",
    ),
    (
        "api_key",
        "provider refused request (api_key=sk-live-9f8e7d6c5b4a)",
        "sk-live-9f8e7d6c5b4a",
        "api_key",
    ),
    (
        "aws_secret_access_key",
        "botocore config dump: aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCY",
        "wJalrXUtnFEMIK7MDENGbPxRfiCY",
        "aws_secret_access_key",
    ),
    (
        "X-Amz-Security-Token header",
        "signature mismatch; X-Amz-Security-Token: FwoGZXIvYXdzEBYaDF9session",
        "FwoGZXIvYXdzEBYaDF9session",
        "X-Amz-Security-Token",
    ),
    (
        "X-DataHub-Token header",
        "403 from GMS with X-DataHub-Token: pat-live-0011223344556677",
        "pat-live-0011223344556677",
        "X-DataHub-Token",
    ),
    (
        "Authorization Basic",
        "401 Unauthorized (sent Authorization: Basic ZGF0YXNwb2tlOnMzY3JldA==)",
        "ZGF0YXNwb2tlOnMzY3JldA==",
        "Authorization",
    ),
    (
        "naked Bearer scheme token",
        "retry gave up; request carried Bearer eyJraWQiOiJhYmMifQ.payload.sig",
        "eyJraWQiOiJhYmMifQ.payload.sig",
        "Bearer",
    ),
    (
        "quoted password",
        "pg auth failed for user dataspoke password='PW1234abcd'",
        "PW1234abcd",
        "password",
    ),
)


@pytest.mark.parametrize(
    ("label", "message", "secret", "name_fragment"),
    _CREDENTIAL_CASES,
    ids=[c[0] for c in _CREDENTIAL_CASES],
)
def test_credential_value_is_redacted_and_its_name_survives(
    label: str, message: str, secret: str, name_fragment: str
) -> None:
    """The credential value is replaced; the credential's name is kept.

    Spec: spec/feature/BACKEND.md §Health reporting — ``last_error`` is
    "credential-free".
    """
    out = sanitize_error_message(message)
    assert out is not None
    assert secret not in out, (
        f"{label}: the credential value {secret!r} must not survive sanitisation; got {out!r}. "
        "Spec: spec/feature/BACKEND.md §Health reporting — last_error is credential-free."
    )
    assert REDACTED in out, (
        f"{label}: the value must be replaced by the redaction marker, not merely dropped, so "
        f"the operator can see something was withheld; got {out!r}."
    )
    assert name_fragment.lower() in out.lower(), (
        f"{label}: the credential *name* {name_fragment!r} must be preserved — a name is not a "
        f"secret, and dropping it makes two different credentials read alike; got {out!r}."
    )


def test_url_userinfo_password_is_removed() -> None:
    """A credential embedded in a URL's userinfo does not survive.

    ``gms_url`` accepts no userinfo through the API, but a message can quote a URL
    assembled anywhere, so the pattern layer covers it regardless.

    Spec: spec/feature/BACKEND.md §Health reporting — ``last_error`` is
    "credential-free".
    """
    out = sanitize_error_message(
        "HTTPError for https://dataspoke:tOpS3cretPass@gms.internal:8080/openapi/v3"
    )
    assert out is not None
    assert "tOpS3cretPass" not in out, (
        f"a URL userinfo password must not survive; got {out!r}"
    )
    # Backstop: the diagnostic remainder is intact, so the scrub did not blank the message.
    assert "gms.internal" in out and "/openapi/v3" in out, (
        f"the host and path carry the diagnostic value and must survive; got {out!r}"
    )


def test_each_parameter_of_a_multi_credential_query_string_is_redacted() -> None:
    """Two credentials in one query string are each redacted, and the middle
    non-credential parameter survives.

    A value class that swallowed ``&`` would redact from the first credential to the
    end of the string, taking the ordinary parameters with it and leaving the second
    credential's name invisible.

    Spec: spec/feature/BACKEND.md §Health reporting — ``last_error`` is
    "credential-free".
    """
    out = sanitize_error_message(
        "POST /gms?access_token=AAAAAAAAAAAA&entity=dataset&client_secret=BBBBBBBBBBBB → 502"
    )
    assert out is not None
    assert "AAAAAAAAAAAA" not in out and "BBBBBBBBBBBB" not in out, (
        f"both credential values must be redacted independently; got {out!r}"
    )
    assert out.count(REDACTED) == 2, (
        f"each credential parameter must be redacted separately (expected 2 markers); got {out!r}"
    )
    assert "entity=dataset" in out, (
        f"the non-credential parameter between them must survive; got {out!r}"
    )


@pytest.mark.parametrize(
    ("label", "splice"),
    [
        ("NUL", "\x00"),
        ("zero-width space", "​"),
        ("soft hyphen", "\xad"),
        ("newline", "\n"),
        ("carriage return", "\r"),
        ("tab", "\t"),
    ],
)
def test_a_secret_tampered_with_mid_value_is_still_scrubbed(label: str, splice: str) -> None:
    """An invisible or whitespace character spliced into the credential does not
    smuggle it through the exact-value scrub.

    Normalisation runs first — the invisible characters are dropped and the
    whitespace ones collapse to a space — and the exact-value match is
    space-tolerant, so the credential is matched whole either way.

    Spec: spec/feature/BACKEND.md §Health reporting — ``last_error`` is
    "credential-free"; the property must not depend on the transport quoting the
    credential unmodified.
    """
    secret = "pat-live-0011223344556677"
    tampered = secret[:10] + splice + secret[10:]
    message = f"GMS rejected the request; token was {tampered}"
    # Backstop: the fixture really does carry a tampered value, so a passing result
    # cannot come from the plain secret happening to be present.
    assert secret not in message, (
        f"{label}: the fixture must not contain the untampered secret, or the scrub is "
        "trivially exercised."
    )

    out = sanitize_error_message(message, secrets=(secret,))
    assert out is not None
    assert secret not in out, (
        f"{label}: normalisation must not reassemble the credential intact; got {out!r}"
    )
    for fragment in (secret[:10], secret[10:]):
        assert fragment not in out, (
            f"{label}: no half of the credential may survive either; {fragment!r} in {out!r}"
        )
    assert REDACTED in out


def test_control_and_format_characters_are_stripped() -> None:
    """Terminal escapes, bidirectional overrides, NUL and newlines never reach a sink.

    ``last_error`` is read back over ``curl``, the same text lands in a line-based log
    collector, and a PostgreSQL ``text`` column rejects ``NUL`` outright — turning a
    redaction problem into a lost row.

    Spec: spec/feature/BACKEND.md §Health reporting — a persisted message "cannot
    become a disclosure or log-forging surface".
    """
    message = (
        "GMS \x1b[31mreturned\x1b[0m 500\nsecond line\rthird\ttab"
        "‮overridden​zero-width\x00nul"
    )
    out = sanitize_error_message(message)
    assert out is not None
    for label, char in (
        ("ESC", "\x1b"),
        ("newline", "\n"),
        ("carriage return", "\r"),
        ("tab", "\t"),
        ("RTL override", "‮"),
        ("zero-width space", "​"),
        ("NUL", "\x00"),
    ):
        assert char not in out, f"{label} must not survive sanitisation; got {out!r}"
    # Backstop: the words either side of every stripped character are still readable
    # and not spliced together, so stripping did not destroy the message.
    assert "GMS" in out and "returned" in out and "500 second line third tab" in out, (
        f"the diagnostic words must survive, unspliced; got {out!r}"
    )


# ── Diagnostics that must survive verbatim ───────────────────────────────────

_DIAGNOSTIC_CASES: tuple[tuple[str, str], ...] = (
    (
        "dataset URN in a GMS fetch error",
        "Unable to fetch entity with key: "
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    ),
    ("KeyError on a dict key named like a field", "KeyError: 'dataset_urn'"),
    ("a path that merely looks like a credential", "cannot open /etc/passwd: permission denied"),
    ("primary_key column report", "primary_key=id mismatch"),
    ("cassandra keyspace", "table keyspace=analytics not found"),
    ("redshift sortkey", "sortkey: created_at is not a column"),
    ("requests connection pool repr", "HTTPSConnectionPool(host='gms.internal', port=8080)"),
    ("elasticsearch analyzer setting", "tokenizer=whitespace"),
)


@pytest.mark.parametrize(
    ("label", "message"), _DIAGNOSTIC_CASES, ids=[c[0] for c in _DIAGNOSTIC_CASES]
)
def test_diagnostic_message_survives_verbatim(label: str, message: str) -> None:
    """A message carrying no credential is returned unchanged, character for character.

    These are the non-matching side of the filter: each contains a word that reads
    like a credential name (``key``, ``passwd``, ``keyspace``, ``sortkey``,
    ``tokenizer``) or punctuation the URL/header patterns key on, and none of them is
    a secret. An over-broad pattern that redacted any of them would destroy the only
    operator signal the reporting subsystem has.

    Spec: spec/feature/BACKEND.md §Health reporting — the row is "the persisted,
    operator-facing record (``last_error``, ``last_ok_at``) rendered beside the
    configuration that caused the fault".
    """
    assert sanitize_error_message(message) == message, (
        f"{label}: a credential-free diagnostic must survive verbatim; "
        f"got {sanitize_error_message(message)!r}"
    )


def test_credential_name_is_preserved_so_two_names_stay_distinguishable() -> None:
    """``access_token`` and ``token`` redact to distinguishable outputs.

    Spec: spec/feature/BACKEND.md §Health reporting — ``last_error`` is the
    operator-facing record "rendered beside the configuration that caused the fault",
    which requires naming *which* credential was rejected.
    """
    specific = sanitize_error_message("rejected: access_token=AAAAAAAAAAAA")
    generic = sanitize_error_message("rejected: token=AAAAAAAAAAAA")
    assert specific is not None and generic is not None
    assert specific != generic, (
        f"the credential name must survive so the two reports differ; both read {specific!r}"
    )
    assert "access_token" in specific and "AAAAAAAAAAAA" not in specific
    assert "token" in generic and "AAAAAAAAAAAA" not in generic


# ── Degenerate inputs ────────────────────────────────────────────────────────


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t"])
def test_empty_message_becomes_none_so_callers_can_store_null(empty: str | None) -> None:
    """An empty or whitespace-only message returns ``None``, not ``""``.

    ``report_peripheral_health`` stores the result directly, and SQL ``NULL`` is the
    honest encoding of "no message" — an empty string reads as a reported message
    that happens to be blank.

    Spec: spec/feature/BACKEND.md §Health reporting — ``unknown``/absent and a
    reported value are distinct readings of the row.
    """
    assert sanitize_error_message(empty) is None


def test_degenerate_secrets_are_ignored_rather_than_corrupting_the_message() -> None:
    """``None`` and ``""`` in ``secrets`` are skipped; the message survives intact.

    A caller assembles ``secrets`` from optional config (``DataHubClient`` passes its
    token and the GMS URL's password, either of which may be unset), so falsy entries
    reach this function in normal operation. Substituting on an empty pattern would
    shred every message.
    """
    message = "GMS returned 503 Service Unavailable"
    assert sanitize_error_message(message, secrets=(None, "")) == message


def test_a_secret_equal_to_the_whole_message_redacts_to_the_marker() -> None:
    """When the entire message is the credential, the output is just the marker.

    Nothing survives to leak, and the marker (rather than ``None``) tells the
    operator a message existed and was withheld.
    """
    secret = "pat-live-0011223344556677"
    assert sanitize_error_message(secret, secrets=(secret,)) == REDACTED


@pytest.mark.parametrize(
    ("label", "sizes"),
    [
        # Both inputs sit *under* the outer input bound, so this pair is unsatisfiable
        # unless the inner truncation is applied: without `[:_MAX_SCAN_LENGTH]` the two
        # outputs would be 10 000 and 60 000 characters long.
        ("both under the outer bound", (10_000, 60_000)),
        # Far larger inputs, one either side of the outer input bound. Not a second
        # discriminator — see the docstring: the outer bound is a cost bound only, so
        # this pair is decided by the same inner slice as the first.
        ("both far above it", (100_000, 1_000_000)),
    ],
)
def test_output_length_is_bounded_regardless_of_input_length(
    label: str, sizes: tuple[int, int]
) -> None:
    """The inner scan bound clamps the returned text, decoupling output from input length.

    No caller bounds what a transport hands back, and every sink is narrower than that.
    What is proven here is exactly one thing: ``[:_MAX_SCAN_LENGTH]`` clamps the returned
    text, so two inputs of different sizes yield the same output length and that length
    respects the documented inner ceiling. The constant is imported rather than restated
    so tightening it stays a one-place change.

    **The outer ``_MAX_INPUT_LENGTH`` bound is not observable here, and this test does not
    claim it is.** It is a *cost* bound — it caps how much text normalization and the
    exact-value scrub walk — and the inner slice runs after it, so deleting
    ``[:_MAX_INPUT_LENGTH]`` outright leaves every case below green. Both parametrize
    cases are therefore decided by the inner slice alone; the second is kept only as a
    larger-magnitude smoke case, not as a second discriminator. Whether the outer bound
    exists is a question about work performed, which output length cannot answer.

    Spec: spec/feature/BACKEND.md §Health reporting — a persisted message carries "no
    credentials, no stack traces, and **a length bound**, so a persisted message cannot
    become a disclosure or log-forging surface".
    """
    outputs = [sanitize_error_message("x" * size) for size in sizes]
    assert outputs[0] is not None and outputs[1] is not None
    assert len(outputs[0]) == len(outputs[1]), (
        f"{label}: output length must be decoupled from input length; got "
        f"{len(outputs[0])} for a {sizes[0]}-char input vs {len(outputs[1])} for "
        f"{sizes[1]}"
    )
    assert len(outputs[0]) <= _MAX_SCAN_LENGTH, (
        f"{label}: the returned text must respect the inner scan bound "
        f"({_MAX_SCAN_LENGTH}); got {len(outputs[0])}"
    )


def test_a_dense_credential_name_input_terminates_and_is_bounded() -> None:
    """~100 kB of delimiter-dense credential-name fragments terminates and returns bounded text.

    What this proves: the function completes on an input built to maximise the number of
    positions at which the credential-name pattern can start, and its output respects the
    inner bound. Every layer runs synchronously on the API's event loop, so a call that
    did not return would stall all request handling until the liveness probe restarted the
    pod.

    What it does **not** prove, stated so the test is not read as more than it is: that
    the pattern's affix quantifiers are bounded. Replacing ``{0,60}`` with ``*`` in
    ``_NAME_PREFIX`` and ``_NAME_SUFFIX`` leaves this test green — no super-linear
    behaviour is observable either way on this input class. Bounding *pattern execution
    time* against a genuinely adversarial input is issue **#114**, and it is not yet
    closed; nothing here should be taken as covering it. The 1 s ceiling is a
    did-it-hang guard, not a performance target, and is deliberately far above the
    measured cost so it cannot become an impl-pinning assertion.
    """
    unit = ".t.k.s.p.c.token.key.secret.pwd"
    adversarial = unit * (100_000 // len(unit))
    assert len(adversarial) > 90_000, "the fixture must actually be ~100 kB"

    started = time.perf_counter()
    out = sanitize_error_message(adversarial)
    elapsed = time.perf_counter() - started

    assert out is not None
    assert len(out) <= _MAX_SCAN_LENGTH, (
        f"the returned text must respect the inner scan bound; got {len(out)}"
    )
    assert elapsed < 1.0, (
        f"sanitising a {len(adversarial)}-char dense input took {elapsed:.3f}s — the call "
        "must return rather than hang the event loop."
    )
