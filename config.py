from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory for user-supplied / writable files (.env, caches, config).

    When frozen by PyInstaller this is the folder that holds the .exe, so these
    files live next to the binary and survive between runs. In a normal Python
    run it's the source directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Directory for read-only bundled assets (e.g. the mascot PNG).

    PyInstaller unpacks bundled data to a temp dir exposed as ``sys._MEIPASS``.
    Outside a frozen build it's just the source directory.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load a sibling `.env` file into os.environ if present.

    Uses python-dotenv when installed; otherwise falls back to a tiny built-in
    parser so the app runs with zero extra dependencies. Existing environment
    variables always win (we only set defaults).
    """
    env_path = app_dir() / ".env"
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass

    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    """Runtime configuration sourced from environment variables."""

    CLIENT_ID: str = _get("CLAUDE_CLIENT_ID")
    USAGE_URL: str = _get(
        "CLAUDE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
    TOKEN_URL: str = _get(
        "CLAUDE_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token")
    OAUTH_BETA: str = _get("CLAUDE_OAUTH_BETA", "oauth-2025-04-20")

    # --- rate limiting for the /usage endpoint -------------------------------
    MIN_LIMITS_INTERVAL: int = _get_int("CLAUDE_MIN_LIMITS_INTERVAL", 120)
    BACKOFF_BASE: int = _get_int("CLAUDE_BACKOFF_BASE", 300)
    BACKOFF_MAX: int = _get_int("CLAUDE_BACKOFF_MAX", 1800)

    def validate(self) -> None:
        """Raise a clear error if a required value is missing."""
        if not self.CLIENT_ID:
            raise RuntimeError(
                "CLAUDE_CLIENT_ID is not set. Copy .env.example to .env and "
                "fill it in (see the README).")


config = Config()
