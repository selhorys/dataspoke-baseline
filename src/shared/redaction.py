"""Redaction for operator-facing error strings.

A transport exception's ``str()`` is not a DataSpoke-authored message: it can
quote the request that failed — a URL carrying userinfo, an ``Authorization``
header, or a config mapping holding a credential. Those strings reach several
sinks, each read by a different population: the ``peripheral_health.last_error``
column an Admin reads back over the API, the internal activity's ``500`` body
that Airflow records in its task logs, and the API's own application logs.

``sanitize_error_message`` is the single control applied to all of them. The
layers run in this order, and the order is load-bearing:

1. **Whitespace collapse and control-character strip** — first, because it is
   also a *normalization* step that the layer below depends on. A message must
   not carry newlines (log forging), terminal escapes (``last_error`` is read
   back over ``curl``), bidirectional overrides (which render the message in an
   order its reporter did not produce), or ``NUL`` — which a PostgreSQL ``text``
   column rejects outright, turning a redaction problem into a *lost* row.
2. **Exact-value scrub** — the strongest layer, and the reason the function takes
   a ``secrets`` argument: a caller holding the live credential
   (``DataHubClient`` knows its own token) matches it exactly, where a pattern can
   only guess at its shape. It runs *after* normalization because a single
   invisible character spliced into the credential would defeat an exact match
   made before the strip, and the strip would then reassemble the secret intact.
3. **Pattern scrub** — defence in depth for credentials no caller holds the value
   of: URL userinfo, ``Authorization`` headers, and ``<name>=<value>`` /
   ``"<name>": "<value>"`` pairs whose name reads like a credential.

Layer 3 is deliberately conservative about *what* it treats as a credential name.
These messages are the only operator signal for the subsystems that report them,
so a pattern that eats the URN out of ``Unable to fetch entity with key: urn:…``
costs more than it protects. The credential word must therefore be a whole
delimiter-separated segment of the name, generic words that read as credentials
far more often than not (bare ``key``, ``passwd``) are excluded in favour of the
specific forms that actually name secrets, and the matched **name** is preserved
in the output — only the value is replaced. A name is not a secret, and keeping
it is what makes ``?access_token=`` distinguishable from ``?token=``.

Input is capped so no layer can be handed an unbounded message — see
``_MAX_INPUT_LENGTH`` / ``_MAX_SCAN_LENGTH`` for why that takes two bounds rather
than one. Each sink still owns its own output bound (``peripheral_health``
truncates to ``_MAX_ERROR_LENGTH``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

REDACTED = "<redacted>"

# Below this length a credential is not scrubbed by value at all, and layer 3 is
# no backstop for it — `pw=adm1n` names nothing this module recognises, so a short
# secret under a short name is reported verbatim. Accepted: substituting a
# 3-character string would hit every unrelated occurrence of those characters and
# leave a message less usable than the leak it prevents. A credential this short
# is a provisioning problem, not a redaction one.
_MIN_SECRET_LENGTH = 6

# Two bounds, because truncation and redaction pull against each other. Nothing
# here may be handed an unbounded message: every layer is at least linear with a
# Python-level per-character constant, on the API's event loop, and no caller
# bounds what a transport handed back.
#
# `_MAX_INPUT_LENGTH` bounds normalization and the exact-value scrub. It is set far
# above the longest credential the API accepts (`token` is `max_length=8192`) on
# purpose: truncating *before* the exact scrub would cut a long credential in half
# and leave the surviving prefix — most of the secret — in the output. Cutting
# after it means a secret anywhere in this window is replaced whole.
#
# `_MAX_SCAN_LENGTH` bounds the pattern layer and the returned text. It is much
# tighter because that layer's per-character constant is the worst of the three,
# and it is past the point where any sink keeps the text (the health row keeps
# 1 KiB).
_MAX_INPUT_LENGTH = 65536
_MAX_SCAN_LENGTH = 8192

# Bounded rather than open-ended, so the two quantified affixes cannot probe each
# other indefinitely. The bound is a hard cliff, not a soft one: a name with more
# than 60 `[\w.-]` characters before the credential word bypasses the pattern
# entirely. Accepted — that shape does not occur in transport messages, and the
# exact-value layer does not depend on this pattern.
_NAME_PREFIX = r"(?:[\w.-]{0,60}[-_.])?"
_NAME_SUFFIX = r"(?:[-_.][\w.-]{0,60})?"

# The credential word, which must occupy a whole `[-_.]`-delimited segment of the
# name. Bare `key` and `passwd` are deliberately absent: they read as ordinary
# identifiers (`primary_key`, `sortkey`, `/etc/passwd`, `KeyError`) far more often
# than as secrets, so the specific `*_key` credential forms are enumerated
# instead.
_CREDENTIAL_NAME = (
    r"(?:token|secret|password|pwd|credential|apikey"
    r"|api[-_.]?key|access[-_.]?key|secret[-_.]?key|private[-_.]?key)"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo: scheme://user:pass@host → scheme://host. The class admits
    # '@' so a password that itself contains '@' backtracks to the LAST '@'
    # before the path rather than stopping at the first and leaving the tail
    # exposed. `[^/\s]` still stops at the path separator, so an ordinary
    # `http://gms:8080/health` (no '@') is left untouched. Consequence of taking
    # the last '@': `https://user:pass@a.com@b.com/p` renders as
    # `https://b.com/p`, naming a host the request never reached — the safe
    # direction, since the alternative leaks the credential.
    (re.compile(r"(?<=://)[^/\s]*@"), ""),
    # An Authorization header, with or without its scheme token.
    (
        re.compile(r"(?i)\bauthorization\b(\s*[=:]\s*)[\"']?\S+(?:\s+\S+)?"),
        r"Authorization\g<1>" + REDACTED,
    ),
    # A naked credential-bearing scheme token.
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\g<1> " + REDACTED),
    # <name>=<value> and "<name>": "<value>" where <name> reads like a credential.
    # The name is matched with optional affix segments so `access_token`,
    # `client_secret` and `X-DataHub-Token` are all caught — `\b` cannot express
    # that, because no boundary exists between `_` and `t`. Each affix must abut a
    # `[-_.]` delimiter and `(?![\w])` closes the name, so the credential word is
    # a whole segment: `tokenizer`, `sortkey` and `keyspace` do not match. The
    # leading `(?<![\w.-])` includes `.` so a dotted name is one start position
    # rather than one per segment. The value class excludes `&` so each parameter
    # of a query string is redacted separately instead of the first match
    # swallowing the rest.
    (
        re.compile(
            rf"(?i)(?<![\w.-])(?P<name>{_NAME_PREFIX}{_CREDENTIAL_NAME}{_NAME_SUFFIX})"
            rf"(?![\w])[\"']?(?P<sep>\s*[=:]\s*)[\"']?[^\"'\s,;}}\]&]+"
        ),
        r"\g<name>\g<sep>" + REDACTED,
    ),
)


def _space_tolerant(secret: str) -> re.Pattern[str]:
    """Compile *secret* so it matches with any run of spaces spliced between chars.

    Built per call rather than cached: a module-level cache would hold plaintext
    credentials for the process lifetime, and the construction is linear in a
    credential's length on a path that only runs when something already failed.
    """
    return re.compile(" *".join(re.escape(c) for c in secret))


def sanitize_error_message(
    message: str | None,
    *,
    secrets: Iterable[str] = (),
) -> str | None:
    """Return *message* with credentials and control characters removed.

    ``secrets`` are scrubbed by exact value, tolerating spaces spliced between
    their characters — pass the live credential values when the caller holds them.
    Returns ``None`` for an empty or fully-redacted-away message so callers can
    store SQL ``NULL``.
    """
    if not message:
        return None

    # 1. Normalize, over the outer bound. '\n' and '\t' are not `isprintable()`, so
    #    collapsing whitespace must precede the control strip or adjacent words get
    #    spliced together.
    text = " ".join(str(message)[:_MAX_INPUT_LENGTH].split())
    text = "".join(
        c for c in text if c == " " or (c.isprintable() and unicodedata.category(c) != "Cf")
    )

    # 2. Exact-value scrub, on the normalized text, matched space-tolerantly.
    #    Normalization can leave a space *inside* the credential — an invisible
    #    character spliced into it (NUL, ZWSP, a soft hyphen) is dropped outright,
    #    but a newline becomes a space — so a literal `str.replace` of the raw
    #    value would miss precisely the message that carried the tampering.
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            text = _space_tolerant(secret).sub(REDACTED, text)

    # 3. Pattern scrub, over the inner bound — safe to truncate here, because any
    #    credential whose value a caller knows is already gone.
    text = text[:_MAX_SCAN_LENGTH]
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)

    return text or None
