"""Unit tests for peripheral configuration Pydantic schema validation.

Concerns covered:

1. DatahubPeripheralPatchRequest:
   - All fields optional; empty request is valid.
   - gms_url and kafka_brokers respect max_length=512.
   - token respects max_length=8192.
   - exclude_unset=True excludes fields not provided.
   - explicit token="" is preserved by exclude_unset dump (clear semantics).

2. LangfusePeripheralPatchRequest:
   - All fields optional; empty request is valid.
   - host and public_key respect max_length=512.
   - secret_key respects max_length=8192.
   - exclude_unset=True excludes fields not provided.
   - explicit secret_key="" is preserved by exclude_unset dump.

Spec traceability:
- src/api/schemas/admin.py DatahubPeripheralPatchRequest — field bounds.
- src/api/schemas/admin.py LangfusePeripheralPatchRequest — field bounds.
- spec/API.md §Admin (/admin) — PATCH is partial (omitted secret = unchanged);
  explicit "" clears the Secret.
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.admin import (
    DatahubPeripheralPatchRequest,
    LangfusePeripheralPatchRequest,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _dh_valid(**kwargs) -> DatahubPeripheralPatchRequest:
    return DatahubPeripheralPatchRequest(**kwargs)


def _dh_invalid(**kwargs) -> None:
    with pytest.raises(ValidationError):
        DatahubPeripheralPatchRequest(**kwargs)


def _lf_valid(**kwargs) -> LangfusePeripheralPatchRequest:
    return LangfusePeripheralPatchRequest(**kwargs)


def _lf_invalid(**kwargs) -> None:
    with pytest.raises(ValidationError):
        LangfusePeripheralPatchRequest(**kwargs)


# ── DatahubPeripheralPatchRequest ─────────────────────────────────────────────


class TestDatahubPeripheralPatchRequestEmpty:
    """Empty request with all fields omitted is valid.

    spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest — all fields optional.
    """

    def test_empty_request_is_valid(self) -> None:
        req = _dh_valid()
        assert req.gms_url is None
        assert req.kafka_brokers is None
        assert req.token is None

    def test_exclude_unset_empty_request_yields_empty_dict(self) -> None:
        """An empty request produces an empty exclude_unset dump.

        spec: API.md §Admin (/admin) — PATCH is partial; omitted field = unchanged;
        no fields provided → nothing patched.
        """
        req = DatahubPeripheralPatchRequest()
        dump = req.model_dump(exclude_unset=True)
        assert dump == {}, f"Empty request must produce empty dump; got {dump}"


class TestDatahubGmsUrlBounds:
    """gms_url: max_length=512.

    spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest.
    """

    def test_gms_url_at_max_length_accepted(self) -> None:
        url = "http://gms:" + "x" * 500  # well within 512
        req = _dh_valid(gms_url=url)
        assert req.gms_url == url

    def test_gms_url_over_512_chars_rejected(self) -> None:
        url = "http://" + "x" * 1000  # 7 + 1000 = 1007 chars — clearly over max_length=512
        _dh_invalid(gms_url=url)

    def test_gms_url_empty_string_accepted(self) -> None:
        req = _dh_valid(gms_url="")
        assert req.gms_url == ""

    def test_gms_url_none_accepted(self) -> None:
        req = _dh_valid(gms_url=None)
        assert req.gms_url is None


class TestDatahubKafkaBrokersBounds:
    """kafka_brokers: max_length=512.

    spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest.
    """

    def test_kafka_brokers_at_max_length_accepted(self) -> None:
        brokers = "kafka:" + "9" * 500  # well within 512
        req = _dh_valid(kafka_brokers=brokers)
        assert req.kafka_brokers == brokers

    def test_kafka_brokers_over_512_chars_rejected(self) -> None:
        brokers = "kafka://" + "x" * 1000  # 8 + 1000 = 1008 chars — clearly over max_length=512
        _dh_invalid(kafka_brokers=brokers)


class TestDatahubTokenBounds:
    """token: max_length=8192.

    spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest.
    """

    def test_token_at_max_length_accepted(self) -> None:
        token = "t" * 8192
        req = _dh_valid(token=token)
        assert req.token == token

    def test_token_over_8192_chars_rejected(self) -> None:
        token = "t" * 8193
        _dh_invalid(token=token)

    def test_token_empty_string_accepted(self) -> None:
        """token="" is valid — represents an explicit clear operation.

        spec: API.md §Admin (/admin) — explicit "" clears the K8s Secret.
        """
        req = _dh_valid(token="")
        assert req.token == ""

    def test_token_none_accepted(self) -> None:
        """token=None is valid — represents "leave unchanged".

        spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest — token default None.
        """
        req = _dh_valid(token=None)
        assert req.token is None


class TestDatahubExcludeUnsetSemantics:
    """model_dump(exclude_unset=True) preserves only explicitly supplied fields.

    The router calls exclude_unset=True WITHOUT exclude_none so that an explicit
    token="" is preserved (clear operation distinguishable from "omit").

    spec: src/api/routers/admin.py — all_updates = body.model_dump(exclude_unset=True).
    """

    def test_provided_fields_appear_in_dump(self) -> None:
        req = DatahubPeripheralPatchRequest(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
        dump = req.model_dump(exclude_unset=True)
        assert "gms_url" in dump
        assert "kafka_brokers" in dump
        assert "token" not in dump

    def test_explicit_token_empty_string_preserved(self) -> None:
        """token="" must survive exclude_unset dump so the router can detect a clear op.

        spec: API.md §Admin (/admin) — set_datahub_token("") is triggered by
        an explicit "" field; omitting the field must not reach set_datahub_token at all.
        """
        req = DatahubPeripheralPatchRequest(token="")
        dump = req.model_dump(exclude_unset=True)
        assert "token" in dump, "token='' must appear in exclude_unset dump (clear semantics)."
        assert dump["token"] == ""

    def test_token_omitted_does_not_appear_in_dump(self) -> None:
        """Omitting token means 'leave unchanged' — must not appear in the dump."""
        req = DatahubPeripheralPatchRequest(gms_url="http://gms:8080")
        dump = req.model_dump(exclude_unset=True)
        assert "token" not in dump


# ── LangfusePeripheralPatchRequest ────────────────────────────────────────────


class TestLangfusePeripheralPatchRequestEmpty:
    """Empty request with all fields omitted is valid.

    spec: src/api/schemas/admin.py LangfusePeripheralPatchRequest — all fields optional.
    """

    def test_empty_request_is_valid(self) -> None:
        req = _lf_valid()
        assert req.host is None
        assert req.public_key is None
        assert req.secret_key is None

    def test_exclude_unset_empty_request_yields_empty_dict(self) -> None:
        req = LangfusePeripheralPatchRequest()
        dump = req.model_dump(exclude_unset=True)
        assert dump == {}


class TestLangfuseHostBounds:
    """host: max_length=512.

    spec: src/api/schemas/admin.py LangfusePeripheralPatchRequest.
    """

    def test_host_at_max_length_accepted(self) -> None:
        host = "http://" + "h" * 498  # 7 + 498 = 505 chars
        req = _lf_valid(host=host)
        assert req.host == host

    def test_host_over_512_chars_rejected(self) -> None:
        host = "http://" + "h" * 1000  # 7 + 1000 = 1007 chars — clearly over max_length=512
        _lf_invalid(host=host)

    def test_host_empty_string_accepted(self) -> None:
        req = _lf_valid(host="")
        assert req.host == ""


class TestLangfusePublicKeyBounds:
    """public_key: max_length=512.

    spec: src/api/schemas/admin.py LangfusePeripheralPatchRequest.
    """

    def test_public_key_at_max_length_accepted(self) -> None:
        key = "pk-" + "k" * 509  # 3 + 509 = 512 chars
        req = _lf_valid(public_key=key)
        assert req.public_key == key

    def test_public_key_over_512_chars_rejected(self) -> None:
        key = "pk-" + "k" * 1000  # 3 + 1000 = 1003 chars — clearly over max_length=512
        _lf_invalid(public_key=key)


class TestLangfuseSecretKeyBounds:
    """secret_key: max_length=8192.

    spec: src/api/schemas/admin.py LangfusePeripheralPatchRequest.
    """

    def test_secret_key_at_max_length_accepted(self) -> None:
        key = "s" * 8192
        req = _lf_valid(secret_key=key)
        assert req.secret_key == key

    def test_secret_key_over_8192_chars_rejected(self) -> None:
        key = "s" * 8193
        _lf_invalid(secret_key=key)

    def test_secret_key_empty_string_accepted(self) -> None:
        """secret_key="" is valid — represents an explicit clear operation.

        spec: API.md §Admin (/admin) — explicit "" clears the K8s Secret.
        """
        req = _lf_valid(secret_key="")
        assert req.secret_key == ""

    def test_secret_key_none_accepted(self) -> None:
        """secret_key=None is valid — represents "leave unchanged"."""
        req = _lf_valid(secret_key=None)
        assert req.secret_key is None


class TestLangfuseExcludeUnsetSemantics:
    """model_dump(exclude_unset=True) preserves only explicitly supplied fields.

    spec: src/api/routers/admin.py — all_updates = body.model_dump(exclude_unset=True).
    """

    def test_provided_fields_appear_in_dump(self) -> None:
        req = LangfusePeripheralPatchRequest(host="http://langfuse:3000", public_key="pk-test")
        dump = req.model_dump(exclude_unset=True)
        assert "host" in dump
        assert "public_key" in dump
        assert "secret_key" not in dump

    def test_explicit_secret_key_empty_string_preserved(self) -> None:
        """secret_key="" must survive exclude_unset dump so the router detects a clear op.

        spec: API.md §Admin (/admin) — set_langfuse_secret_key("") triggered by "".
        """
        req = LangfusePeripheralPatchRequest(secret_key="")
        dump = req.model_dump(exclude_unset=True)
        assert "secret_key" in dump, "secret_key='' must appear in exclude_unset dump."
        assert dump["secret_key"] == ""

    def test_secret_key_omitted_does_not_appear_in_dump(self) -> None:
        req = LangfusePeripheralPatchRequest(host="http://langfuse:3000")
        dump = req.model_dump(exclude_unset=True)
        assert "secret_key" not in dump
