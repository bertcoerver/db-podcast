"""Configuration, from environment variables or a ``.env`` file.

Everything is an environment variable so that the same code runs unchanged
from a shell on a laptop and from a Home Assistant add-on, which hands its
options to the container as environment.
"""

import os
from dataclasses import dataclass
from pathlib import Path

APP_KEY_ENV = "DB_PODCAST_APP_KEY"
REFRESH_TOKEN_ENV = "DB_PODCAST_REFRESH_TOKEN"
LIBRARY_ENV = "DB_PODCAST_LIBRARY"
REMOTE_ROOT_ENV = "DB_PODCAST_REMOTE_ROOT"
FEEDS_ENV = "DB_PODCAST_FEEDS"

DEFAULT_LIBRARY = "~/fetchit"
DEFAULT_REMOTE_ROOT = "/podcasts"
ENV_FILE = ".env"


class ConfigError(Exception):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    """Runtime configuration."""

    library: Path
    remote_root: str
    app_key: str | None
    refresh_token: str | None
    feeds: list[str] | None = None
    """The podcasts to publish, by name; ``None`` for every one in the library."""

    def credentials(self) -> tuple[str, str]:
        """Return the app key and refresh token, or say how to get them."""
        if not self.app_key or not self.refresh_token:
            raise ConfigError(
                f"{APP_KEY_ENV} and {REFRESH_TOKEN_ENV} are not set. "
                "Run `db-podcast auth` to connect to Dropbox."
            )
        return self.app_key, self.refresh_token


def get_config(env_file: str | Path = ENV_FILE) -> Config:
    """Build the configuration from the environment, loading ``env_file`` first."""
    load_dotenv(env_file)
    return Config(
        library=Path(os.environ.get(LIBRARY_ENV) or DEFAULT_LIBRARY).expanduser(),
        remote_root=os.environ.get(REMOTE_ROOT_ENV) or DEFAULT_REMOTE_ROOT,
        app_key=os.environ.get(APP_KEY_ENV) or None,
        refresh_token=os.environ.get(REFRESH_TOKEN_ENV) or None,
        feeds=parse_feeds(os.environ.get(FEEDS_ENV)),
    )


def parse_feeds(value: str | None) -> list[str] | None:
    """Read a comma-separated list of podcast names; empty means all of them.

    Commas rather than spaces, because feed names have spaces in them.
    """
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    return names or None


def load_dotenv(path: str | Path = ENV_FILE) -> None:
    """Load ``KEY=VALUE`` lines from a file into the environment.

    Variables that are already set win, and a missing file is not an error.
    """
    for key, value in _read_dotenv(Path(path)).items():
        os.environ.setdefault(key, value)


def save_dotenv(values: dict[str, str], path: str | Path = ENV_FILE) -> None:
    """Set ``values`` in a ``.env`` file, keeping its other lines as they are."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition("=")[0].strip() not in values]
    lines.extend(f"{key}={value}" for key, value in values.items())
    # Created private from the start: the token in it is access to a Dropbox.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if not separator or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values
