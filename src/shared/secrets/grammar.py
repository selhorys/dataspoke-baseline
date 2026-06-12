"""Secret-reference grammar: the single ``${name__key}`` pattern and its parser.

One compiled pattern, shared by both the run-time recipe substitution and the
save-time reference extraction, so the verified set provably matches the
substituted set. This module is stdlib-only.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import re

from src.shared.secrets.interface import SecretRefMalformed

# Matches a ${name__key} secret reference: a DNS-label-safe name segment
# (lowercase alphanumerics and hyphens), the literal __ separator, then a
# Secret data-key segment (alphanumerics, hyphens, underscores, dots).
# A ${...} token that does not match (e.g. an uppercase name, or a DataHub env
# placeholder without __) is left untouched by the substitution.
SECRET_REF_RE = re.compile(r"\$\{([a-z0-9-]+)__([A-Za-z0-9_.-]+)\}")


def parse_name_key(ref: str) -> tuple[str, str]:
    """Split ``name__key`` on the last ``__`` into ``(name, key)``.

    Raises:
        SecretRefMalformed: if ``__`` is absent, or either segment is empty.
    """
    if "__" not in ref:
        raise SecretRefMalformed(
            f"Secret ref {ref!r} has no '__' separator. "
            "Expected format: 'name__key' (e.g. 'team-pg__password')."
        )
    # Split on the LAST ``__`` so ``name`` may contain ``__`` (though DNS-label
    # names cannot, this is a defensive choice consistent with the spec).
    last_sep = ref.rfind("__")
    name = ref[:last_sep]
    key = ref[last_sep + 2:]
    if not name:
        raise SecretRefMalformed(f"Secret ref {ref!r}: name segment is empty.")
    if not key:
        raise SecretRefMalformed(f"Secret ref {ref!r}: key segment is empty.")
    return name, key
