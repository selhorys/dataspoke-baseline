"""DataSpoke CLI — single command to start the full dev stack.

Usage:
    uv run -m src.cli [options]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ── .env loader (from tests/integration/conftest.py) ────────────────────────


def _load_dotenv(env_file: str) -> Path:
    """Load a .env file into os.environ without overwriting existing vars.

    Returns the resolved path to the file.
    """
    path = Path(env_file)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_file():
        print(f"Error: env file not found: {path}", file=sys.stderr)
        sys.exit(1)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
    return path


# ── Alembic URL construction (from tests/integration/conftest.py) ───────────


def _build_alembic_url() -> str:
    host = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
    port = os.environ.get("DATASPOKE_POSTGRES_PORT", "9201")
    user = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
    password = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "dataspoke")
    db = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def _run_alembic() -> None:
    env = {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT), "DATASPOKE_ALEMBIC_URL": _build_alembic_url()}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        print("Error: alembic upgrade failed", file=sys.stderr)
        sys.exit(1)


# ── Banner ───────────────────────────────────────────────────────────────────

_LINE = "\u2500" * 42


def _print_banner(port: int, components: list[str], env_file: Path) -> None:
    kestra_url = os.environ.get("DATASPOKE_KESTRA_URL", "http://localhost:9205")
    lines = [
        "",
        _LINE,
        "  DataSpoke Dev Server",
        _LINE,
        f"  API:       http://localhost:{port}",
        f"  Swagger:   http://localhost:{port}/docs",
        f"  ReDoc:     http://localhost:{port}/redoc",
        f"  Kestra UI: {kestra_url}",
        "",
        f"  Components:  {', '.join(components)}",
        f"  Env:         {env_file}",
        _LINE,
        "  Ctrl+C to stop",
        _LINE,
        "",
    ]
    print("\n".join(lines), flush=True)


# ── Process management ───────────────────────────────────────────────────────


def _start_api(port: int, reload: bool) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, "-m", "uvicorn", "src.api.main:app", "--port", str(port)]
    if reload:
        cmd.append("--reload")
    return subprocess.Popen(cmd, cwd=_PROJECT_ROOT)


def _shutdown(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.monotonic() + 5
    for p in procs:
        remaining = max(0, deadline - time.monotonic())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _wait(procs: list[subprocess.Popen[bytes]]) -> None:
    """Block until any child exits or Ctrl+C is pressed."""
    try:
        while True:
            for p in procs:
                ret = p.poll()
                if ret is not None:
                    print(f"\nProcess {p.args} exited with code {ret}", file=sys.stderr)
                    _shutdown(procs)
                    sys.exit(ret)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
        _shutdown(procs)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dataspoke",
        description="Start the DataSpoke dev stack (default: all components).",
    )
    parser.add_argument("--backend-only", action="store_true", help="Start only backend components (API)")
    parser.add_argument("--skip-migrate", action="store_true", help="Skip Alembic migration")
    parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    parser.add_argument("--no-reload", action="store_true", help="Disable uvicorn auto-reload")
    parser.add_argument("--env-file", default="dev_env/.env", help="Path to .env file (default: dev_env/.env)")
    args = parser.parse_args()

    # 1. Load environment
    env_path = _load_dotenv(args.env_file)

    # 2. Run migrations
    if not args.skip_migrate:
        print("Running alembic upgrade head...")
        _run_alembic()

    # 3. Determine components
    components: list[str] = ["API"]
    if not args.backend_only:
        # Frontend not yet implemented — will be added here
        pass

    # 4. Banner
    _print_banner(args.port, components, env_path)

    # 5. Start processes
    procs: list[subprocess.Popen[bytes]] = []
    procs.append(_start_api(args.port, reload=not args.no_reload))
    if not args.backend_only:
        # procs.append(_start_frontend())  # TODO: add when src/frontend/ is ready
        pass

    # 6. Wait / handle signals
    _wait(procs)


if __name__ == "__main__":
    main()
