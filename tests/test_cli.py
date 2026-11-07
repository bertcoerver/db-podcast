"""Tests for the command-line glue that do not need network access."""

import pytest

from db_podcast import cli
from db_podcast.config import FEEDS_ENV, get_config, parse_feeds


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (" , ", None),
        ("Bureau Buitenland", ["Bureau Buitenland"]),
        ("Bureau Buitenland, Mes premières Belles Histoires", ["Bureau Buitenland", "Mes premières Belles Histoires"]),
    ],
)
def test_parse_feeds(value, expected):
    """Test that the feeds to publish are a comma-separated list, names and all."""
    assert parse_feeds(value) == expected


def test_feeds_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(FEEDS_ENV, "A, B")
    assert get_config(tmp_path / "none.env").feeds == ["A", "B"]


def test_feed_on_the_command_line_wins(monkeypatch, tmp_path):
    """Test that --feed replaces what the environment says, for this run."""
    monkeypatch.setenv(FEEDS_ENV, "A")
    seen = {}

    def fake_sync(library, remote, prune=True, dry_run=False, names=None):
        seen.update(library=library, names=names)
        return []

    monkeypatch.setattr(cli, "_remote", lambda config: object())
    monkeypatch.setattr(cli.sync, "sync", fake_sync)

    assert cli.main(["--library", str(tmp_path), "--feed", "B", "--feed", "C", "sync"]) == 0
    assert seen == {"library": tmp_path, "names": ["B", "C"]}
