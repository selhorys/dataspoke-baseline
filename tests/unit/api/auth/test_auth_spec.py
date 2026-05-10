"""Unit tests for AuthSpec validation matrix.

Every row of the validation matrix in spec/feature/SECRET_RESOLUTION.md
§Validation matrix has exactly one test here.

spec: spec/feature/SECRET_RESOLUTION.md §Validation matrix (API boundary,
      returns 422 INVALID_PARAMETER)
spec: spec/feature/SECRET_RESOLUTION.md §API schema (src/api/schemas/ingestion.py)
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.ingestion import AuthSpec
from src.shared.models.ingestion import CredentialAuth, SecretRefRecord


class TestAuthSpecValidationMatrix:
    # Matrix rows follow spec table order (rows 1-9).

    def test_row_1_username_only_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 1
        # {username} only → 422 — no credential supplied.
        with pytest.raises(ValidationError):
            AuthSpec.model_validate({"username": "u"})

    def test_row_2_password_only_no_secret_ref_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 2
        # {username, password} only (no secret_ref) → 422 — plaintext-only is banned.
        with pytest.raises(ValidationError):
            AuthSpec.model_validate({"username": "u", "password": "p"})

    def test_row_3a_vault_path_password_and_secret_ref_ok(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 3a
        # {username, password, secret_ref: {name, key}} → Vault path — accepted.
        auth = AuthSpec.model_validate(
            {
                "username": "u",
                "password": "p",
                "secret_ref": {"name": "dataspoke-source-cred-x", "key": "k"},
            }
        )
        assert auth.username == "u"
        assert auth.password == "p"
        assert auth.secret_ref is not None
        assert auth.secret_ref.name == "dataspoke-source-cred-x"
        assert auth.secret_ref.key == "k"
        assert auth.secret_ref.force_overwrite is False

    def test_row_3b_vault_path_force_overwrite_true_ok(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 3b
        # {username, password, secret_ref: {name, key, force_overwrite: true}} → Vault path.
        auth = AuthSpec.model_validate(
            {
                "username": "u",
                "password": "p",
                "secret_ref": {"name": "dataspoke-source-cred-x", "key": "k", "force_overwrite": True},
            }
        )
        assert auth.secret_ref is not None
        assert auth.secret_ref.force_overwrite is True

    def test_row_4_reference_path_username_and_secret_ref_only_ok(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 4
        # {username, secret_ref: {name, key}} → Reference path — accepted.
        auth = AuthSpec.model_validate(
            {
                "username": "u",
                "secret_ref": {"name": "dataspoke-source-cred-x", "key": "k"},
            }
        )
        assert auth.password is None
        assert auth.secret_ref is not None
        assert auth.secret_ref.name == "dataspoke-source-cred-x"

    def test_row_5_missing_key_in_secret_ref_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 5
        # {username, password, secret_ref: {name}} (missing key) → 422.
        with pytest.raises(ValidationError):
            AuthSpec.model_validate(
                {
                    "username": "u",
                    "password": "p",
                    "secret_ref": {"name": "dataspoke-source-cred-x"},
                }
            )

    def test_row_6_missing_name_in_secret_ref_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 6
        # {username, password, secret_ref: {key}} (missing name) → 422.
        with pytest.raises(ValidationError):
            AuthSpec.model_validate(
                {
                    "username": "u",
                    "password": "p",
                    "secret_ref": {"key": "k"},
                }
            )

    def test_row_7_secret_ref_as_string_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 7
        # Any shape with secret_ref as a string (legacy) → 422 — must be an object.
        with pytest.raises(ValidationError):
            AuthSpec.model_validate(
                {
                    "username": "u",
                    "secret_ref": "k8s-secret/x/y",
                }
            )

    def test_row_8_reference_path_name_without_prefix_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 8
        # secret_ref.name not matching prefix dataspoke-source-cred- → 422 (reference path).
        with pytest.raises(ValidationError):
            AuthSpec.model_validate(
                {
                    "username": "u",
                    "secret_ref": {"name": "team-pg", "key": "k"},
                }
            )

    def test_row_9_vault_path_name_without_prefix_raises(self) -> None:
        # spec: SECRET_RESOLUTION.md §Validation matrix row 9
        # secret_ref.name not matching prefix dataspoke-source-cred- → 422 (vault path).
        with pytest.raises(ValidationError):
            AuthSpec.model_validate(
                {
                    "username": "u",
                    "password": "p",
                    "secret_ref": {"name": "team-pg", "key": "k"},
                }
            )


class TestCredentialAuthPersistenceModel:
    def test_credential_auth_rejects_password_field(self) -> None:
        # spec: SECRET_RESOLUTION.md §Data Model — the vault-path request shape (with
        # password) is NEVER persisted. CredentialAuth uses extra="forbid" so that
        # code paths which accidentally pass a password field are caught at test time.
        with pytest.raises(ValidationError):
            CredentialAuth.model_validate({"username": "x", "password": "x"})

    def test_credential_auth_accepts_reference_shape(self) -> None:
        # spec: SECRET_RESOLUTION.md §Data Model — persisted form is {username, secret_ref}.
        auth = CredentialAuth.model_validate(
            {
                "username": "readonly",
                "secret_ref": {"name": "dataspoke-source-cred-db", "key": "password"},
            }
        )
        assert auth.username == "readonly"
        assert auth.secret_ref is not None
        assert auth.secret_ref.name == "dataspoke-source-cred-db"
        assert auth.secret_ref.key == "password"

    def test_secret_ref_record_rejects_force_overwrite(self) -> None:
        # spec: SECRET_RESOLUTION.md §Data Model — vault-path's force_overwrite is never
        # persisted; the persistence model SecretRefRecord uses extra="forbid" and must
        # reject it.
        with pytest.raises(ValidationError):
            SecretRefRecord.model_validate(
                {"name": "dataspoke-source-cred-x", "key": "k", "force_overwrite": True}
            )
