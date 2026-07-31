---
name: exc-info-leaks-bind-parameters
description: A "we log only the code, never PII" claim is false wherever exc_info=True is passed — SQLAlchemy stringifies DBAPIError with [parameters: {...}] because the engine sets no hide_parameters
metadata:
  type: feedback
---

When a log call is accompanied by a comment or docstring promising that no
identity/PII is recorded, check every keyword on that call — especially
`exc_info=True` — before believing it.

**Why:** on #83 `_oauth_error_redirect` in `src/api/routers/auth.py` documented
"Only the code and the route path are recorded — never the email, the Google
`sub`, or the exception message", then passed `exc_info=True` on the
non-`DataSpokeError` branch. `src/shared/db/session.py` builds the engine without
`hide_parameters`, so `sqlalchemy.exc.DBAPIError.__str__` renders
`[SQL: …] [parameters: {'sub': …, 'email': 'victim@corp.example'}]` — and that
branch exists precisely for a DB failure mid-callback, i.e. the bind values are
the authenticating user's. structlog is never `configure()`d in `src/`, so the
default ConsoleRenderer prints the whole traceback. `src/shared/redaction.py`
does not help here: it scrubs credential-shaped `name=value` pairs, not emails.

**How to apply:** grep the diff for `exc_info` next to any privacy claim. Prove it
by raising a real `DBAPIError` (`sqlexc.DBAPIError.instance(stmt, params, orig,
Exception, hide_parameters=False)`) inside an `except` and calling the logger —
the rendered text prints. `error_type=type(exc).__name__` alone satisfies the
monitoring need without the payload. Note the mirror-image failure: a probe that
calls the logger *outside* an `except` block silently renders nothing, because
`exc_info=True` reads `sys.exc_info()`.
