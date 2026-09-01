"""Hermetic contract tests for the direct-DataHub GraphQL helper.

The helper is the AI Scaffold's direct-DataHub query primitive (spec:
spec/AI_PLUGIN.md §Architecture).  The mutation gate, local request validation,
confined file input, and size cap below trace to the approved change request;
those hardening details are not otherwise specified in a product spec.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HELPER = ROOT / "plugin" / "bin" / "datahub-graphql"
MAX_BODY_BYTES = 128 * 1024


@pytest.fixture
def helper_environment(tmp_path: Path) -> dict[str, str]:
    """Provide isolated credentials and a curl recorder; never contact DataHub."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-arguments"
    curl_stdin = tmp_path / "curl-config"
    curl_body = tmp_path / "curl-body"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$@\" > \"${CURL_ARGUMENT_LOG:?}\"\n"
        "cat > \"${CURL_STDIN_LOG:?}\"\n"
        "previous=\n"
        "for argument in \"$@\"; do\n"
        "  if [ \"$previous\" = --data-binary ]; then\n"
        "    cat -- \"${argument#@}\" > \"${CURL_BODY_LOG:?}\"\n"
        "  elif [ \"$previous\" = --data-raw ]; then\n"
        "    printf '%s' \"$argument\" > \"${CURL_BODY_LOG:?}\"\n"
        "  fi\n"
        "  previous=$argument\n"
        "done\n"
        "printf '{\\\"data\\\":{}}\\n200'\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    (tmp_path / "home").mkdir()
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "DATAHUB_GMS_URL": "https://datahub.example.test/api/gms",
        "DATAHUB_TOKEN": "test-token_123",
        "CURL_ARGUMENT_LOG": str(curl_log),
        "CURL_STDIN_LOG": str(curl_stdin),
        "CURL_BODY_LOG": str(curl_body),
    }


def _run_helper(
    helper_environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HELPER), *arguments],
        capture_output=True,
        check=False,
        env=helper_environment,
        text=True,
    )


def _curl_was_called(helper_environment: dict[str, str]) -> bool:
    return Path(helper_environment["CURL_ARGUMENT_LOG"]).exists()


def _body(query: str) -> str:
    return json.dumps({"query": query}, separators=(",", ":"))


def test_query_sends_request_without_confirmation(helper_environment: dict[str, str]) -> None:
    """An executable query remains the normal, confirmation-free helper operation.

    spec: spec/AI_PLUGIN.md §Architecture (DataHub GraphQL query primitive);
    approved acceptance criterion: query succeeds without ``--confirm``.
    """
    result = _run_helper(helper_environment, _body("query { search { total } }"))

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)
    assert "Authorization: Bearer test-token_123" in Path(
        helper_environment["CURL_STDIN_LOG"]
    ).read_text(encoding="utf-8")
    assert "test-token_123" not in Path(helper_environment["CURL_ARGUMENT_LOG"]).read_text(
        encoding="utf-8"
    )


def test_documented_style_query_with_numeric_arguments_sends_without_confirmation(
    helper_environment: dict[str, str],
) -> None:
    """Numeric argument values in the documented search shape remain query-safe.

    spec: spec/AI_PLUGIN.md §Architecture (DataHub GraphQL query primitive);
    reviewer criterion: ``start:0`` and ``count:10`` do not cause a local rejection.
    """
    query = (
        "query { search(input: {type: DATASET, query: \"public\", start: 0, count: 10}) "
        "{ total } }"
    )
    result = _run_helper(helper_environment, _body(query))

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)


def test_mutation_without_confirm_is_never_sent(helper_environment: dict[str, str]) -> None:
    """A mutation is blocked before curl unless explicitly confirmed first.

    approved acceptance criterion: mutation without confirmation does not call curl.
    """
    result = _run_helper(helper_environment, _body("mutation { deleteTag(urn: \"u\") }"))

    assert result.returncode == 2
    assert "no --confirm" in result.stderr
    assert not _curl_was_called(helper_environment)


def test_leading_confirm_allows_mutation(helper_environment: dict[str, str]) -> None:
    """The first argument is the deliberate opt-in for a mutation send.

    approved acceptance criterion: a leading ``--confirm`` permits a mutation.
    """
    result = _run_helper(
        helper_environment,
        "--confirm",
        _body("mutation { deleteTag(urn: \"u\") }"),
    )

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)


def test_nonleading_confirm_cannot_send_mutation(helper_environment: dict[str, str]) -> None:
    """Only a first-argument confirmation is a valid mutation opt-in.

    approved acceptance criterion: ``--confirm`` is accepted only in the first position.
    """
    result = _run_helper(
        helper_environment,
        _body("mutation { deleteTag(urn: \"u\") }"),
        "--confirm",
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not _curl_was_called(helper_environment)


@pytest.mark.parametrize(
    "query",
    [
        "query mutationType { search { total } }",
        "query { search { total } } fragment mutation on Dataset { urn }",
        "# mutation { deleteTag }\nquery { search { total } }",
        'query { search(query: "mutation { deleteTag }") { total } }',
        'query { search(query: \"\"\"mutation { deleteTag }\"\"\") { total } }',
    ],
)
def test_mutation_text_outside_operation_tokens_does_not_require_confirm(
    helper_environment: dict[str, str], query: str
) -> None:
    """Identifiers, comments, and literals cannot accidentally become mutation operations.

    approved acceptance criterion: mutation text in identifiers/comments/strings is not gated.
    """
    result = _run_helper(helper_environment, _body(query))

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)


@pytest.mark.parametrize(
    "payload",
    ["{", "[]", "{}", '{"query":null}', '{"query":3}'],
)
def test_malformed_or_wrong_shape_json_is_rejected_before_curl(
    helper_environment: dict[str, str], payload: str
) -> None:
    """Only a JSON object containing a string query can reach DataHub.

    approved acceptance criterion: invalid JSON, arrays, and missing/non-string query are blocked.
    """
    result = _run_helper(helper_environment, payload)

    assert result.returncode == 2
    assert "Invalid GraphQL JSON body" in result.stderr
    assert not _curl_was_called(helper_environment)


@pytest.mark.parametrize(
    "query",
    [
        "query { search { total }",
        'query { search(query: "unterminated) { total } }',
        'query { search(query: \"\"\"unterminated) { total } }',
        'query { search(query: "\\q") { total } }',
        "fragment details on Dataset { urn }",
        "query { 123 }",
        "query { field(arg 1) }",
        "query($x: Int = $y) { field }",
    ],
)
def test_malformed_or_nonexecutable_graphql_is_rejected_before_curl(
    helper_environment: dict[str, str], query: str
) -> None:
    """The classifier fails closed on broken syntax and fragment-only documents.

    approved acceptance criterion: unclosed braces, unterminated strings, and documents
    without an executable operation exit locally without curl.
    """
    result = _run_helper(helper_environment, _body(query))

    assert result.returncode == 2
    assert "Invalid GraphQL JSON body" in result.stderr
    assert not _curl_was_called(helper_environment)


def test_directive_only_inline_fragment_sends_without_confirmation(
    helper_environment: dict[str, str],
) -> None:
    """A directive-only inline fragment is a valid query selection, not a mutation.

    reviewer criterion: ``... @skip(if: true) { name }`` succeeds without ``--confirm``.
    """
    result = _run_helper(helper_environment, _body("query { ... @skip(if: true) { name } }"))

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)


def test_regular_at_path_body_is_sent(
    helper_environment: dict[str, str], tmp_path: Path
) -> None:
    """A normal multi-line regular file is a confined request-body input.

    approved acceptance criterion: regular ``@PATH`` body succeeds.
    """
    body_file = tmp_path / "request.json"
    expected_body = '{\n  "query": "query { search { total } }"\n}\n'
    body_file.write_text(expected_body, encoding="utf-8")

    result = _run_helper(helper_environment, f"@{body_file}")

    assert result.returncode == 0, result.stderr
    assert _curl_was_called(helper_environment)
    assert Path(helper_environment["CURL_BODY_LOG"]).read_text(encoding="utf-8") == expected_body


@pytest.mark.parametrize("path_kind", ["empty", "missing", "symlink", "credential"])
def test_unsafe_at_path_body_is_rejected_before_curl(
    helper_environment: dict[str, str], path_kind: str, tmp_path: Path
) -> None:
    """Empty, absent, symlinked, and credential ``@PATH`` inputs are denied locally.

    approved acceptance criterion: unsafe ``@PATH`` forms do not reach curl.
    """
    if path_kind == "empty":
        argument = "@"
    elif path_kind == "missing":
        argument = f"@{tmp_path / 'absent.json'}"
    elif path_kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(_body("query { search { total } }"), encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        argument = f"@{link}"
    else:
        secret_dir = Path(helper_environment["HOME"]) / ".dataspoke"
        secret_dir.mkdir(parents=True)
        secret = secret_dir / "config.json"
        secret.write_text(_body("query { search { total } }"), encoding="utf-8")
        argument = f"@{secret}"

    result = _run_helper(helper_environment, argument)

    assert result.returncode == 2
    assert not _curl_was_called(helper_environment)


@pytest.mark.parametrize("size, expected_code", [(MAX_BODY_BYTES, 0), (MAX_BODY_BYTES + 1, 2)])
def test_body_size_limit_is_enforced_before_curl(
    helper_environment: dict[str, str], size: int, expected_code: int
) -> None:
    """The client accepts exactly 128 KiB and rejects a single additional byte.

    approved acceptance criterion: the 128 KiB boundary is enforced locally.
    """
    empty_body_size = len(_body("{}"))
    payload = _body("{" + "x" * (size - empty_body_size) + "}")
    assert len(payload.encode()) == size

    result = _run_helper(helper_environment, payload)

    assert result.returncode == expected_code, result.stderr
    assert _curl_was_called(helper_environment) is (expected_code == 0)
    if expected_code:
        assert "Body too large" in result.stderr


def test_oversized_at_path_body_is_rejected_before_curl(
    helper_environment: dict[str, str], tmp_path: Path
) -> None:
    """The size cap applies to a confined file body before it can reach curl.

    approved acceptance criterion: an oversized ``@PATH`` body exits locally without curl.
    """
    empty_body_size = len(_body("{}"))
    payload = _body("{" + "x" * (MAX_BODY_BYTES + 1 - empty_body_size) + "}")
    body_file = tmp_path / "oversized.json"
    body_file.write_text(payload, encoding="utf-8")

    result = _run_helper(helper_environment, f"@{body_file}")

    assert result.returncode == 2
    assert not _curl_was_called(helper_environment)
