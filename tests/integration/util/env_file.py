"""Reading ``helm-charts/.env.dev`` from test code.

The file's real consumer is the shell, via ``set -a && source``. Test code reads
it directly instead, so the two must agree on the same encoding: values are
written by ``env_file_set_var`` (``helm-charts/bin/lib/helpers.sh``), which
single-quotes anything the shell would otherwise act on.

One implementation lives here so a change to that encoding has one place to
land on the reading side, matching the single ``env_file_set_var`` on the
writing side.
"""

import os
from pathlib import Path


def unquote_env_value(value: str) -> str:
    """Reverse the quoting ``env_file_set_var`` applies when it writes a value.

    A value carrying whitespace, ``$``, a backtick or ``#`` is wrapped in single
    quotes with embedded apostrophes escaped as ``'\\''``. ``source`` undoes
    that; a textual reader has to undo it too, or the value arrives still
    wearing its quotes and every comparison against it fails on characters no
    test ever put there.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        return inner.replace("'\\''", "'") if value[0] == "'" else inner
    return value


def load_dotenv(start: Path) -> None:
    """Load ``helm-charts/.env.dev`` into ``os.environ`` without overwriting.

    Searches from ``start`` upward, which handles git worktrees where
    ``helm-charts/.env.dev`` lives in the main worktree. A missing file is not
    an error: an already-exported environment is a valid way to run.
    """
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
        key = key.strip()
        value = unquote_env_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value
