"""Tests for the choices of what to publish, and the sync that follows them."""

import json

import pytest

from db_podcast.library import LibraryError, scan
from db_podcast.selection import Selection, episode_key
from db_podcast.sync import sync

from .test_sync import FakeRemote


def _episode(folder, stem, published):
    (folder / f"{stem}.mp3").write_bytes(stem.encode())
    (folder / f"{stem}.json").write_text(json.dumps({"title": stem, "published": published}))


@pytest.fixture
def library(tmp_path):
    """Two podcasts, of two episodes each."""
    root = tmp_path / "library"
    for name in ("alpha", "beta"):
        folder = root / name
        folder.mkdir(parents=True)
        _episode(folder, "one", "2026-01-01T10:00:00+00:00")
        _episode(folder, "two", "2026-02-01T10:00:00+00:00")
    return root


def _by_name(podcasts):
    return {p.name: [e.audio.name for e in p.episodes] for p in podcasts}


def test_nothing_chosen_publishes_nothing(library, tmp_path):
    choices = Selection.load(tmp_path / "none.json")
    assert choices.choose(scan(library)) == []

    alpha = scan(library)[0]
    choices.set(alpha, published=True)
    assert _by_name(choices.choose(scan(library))) == {"alpha": ["two.mp3", "one.mp3"]}


def test_the_feeds_option_is_the_default_for_podcasts_not_chosen_for(library, tmp_path):
    choices = Selection.load(tmp_path / "none.json", default_feeds=["Beta"])
    assert list(_by_name(choices.choose(scan(library)))) == ["beta"]

    alpha = scan(library)[0]
    choices.set(alpha, published=True)
    assert list(_by_name(choices.choose(scan(library)))) == ["alpha", "beta"]


def test_choices_are_saved_and_read_back(library, tmp_path):
    path = tmp_path / "conf" / "selection.json"
    alpha, beta = scan(library)
    choices = Selection.load(path)
    choices.set(alpha, published=True, episodes={"one.mp3": False})
    choices.set(beta, published=False)
    choices.save()

    again = Selection.load(path)
    assert _by_name(again.choose(scan(library))) == {"alpha": ["two.mp3"]}
    assert not path.with_name("selection.json.tmp").exists()


def test_auto_decides_only_for_episodes_that_arrive_later(library, tmp_path):
    alpha = scan(library)[0]
    choices = Selection.load(tmp_path / "s.json")
    choices.set(alpha, published=True)
    # Switching it off keeps what is published now...
    choices.set(alpha, auto=False)
    assert _by_name(choices.choose([alpha])) == {"alpha": ["two.mp3", "one.mp3"]}

    # ...and what comes later waits to be ticked.
    _episode(library / "alpha", "three", "2026-03-01T10:00:00+00:00")
    alpha = scan(library)[0]
    assert _by_name(choices.choose([alpha])) == {"alpha": ["two.mp3", "one.mp3"]}
    assert [choices.included(alpha, e) for e in alpha.episodes] == [False, True, True]

    choices.set(alpha, auto=True)
    _episode(library / "alpha", "four", "2026-04-01T10:00:00+00:00")
    alpha = scan(library)[0]
    assert _by_name(choices.choose([alpha])) == {"alpha": ["four.mp3", "two.mp3", "one.mp3"]}


def test_choices_for_episodes_that_are_gone_are_let_go(library, tmp_path):
    alpha = scan(library)[0]
    choices = Selection.load(tmp_path / "s.json")
    choices.set(alpha, episodes={"one.mp3": False, "never.mp3": True})
    (library / "alpha" / "one.mp3").unlink()
    choices.set(scan(library)[0])
    assert choices.podcasts["alpha"]["episodes"] == {"two.mp3": True}


def test_episodes_in_season_folders_are_keyed_by_their_path(tmp_path):
    show = tmp_path / "lib" / "show"
    (show / "Season 2026").mkdir(parents=True)
    _episode(show / "Season 2026", "ep", "2026-01-01T10:00:00+00:00")
    (podcast,) = scan(tmp_path / "lib")
    assert episode_key(podcast, podcast.episodes[0]) == "Season 2026/ep.mp3"


def test_a_broken_file_is_an_error_not_an_empty_choice(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    with pytest.raises(LibraryError):
        Selection.load(path)


def test_sync_takes_out_what_is_unticked_and_switched_off(library, tmp_path):
    remote = FakeRemote()
    path = tmp_path / "s.json"
    sync(library, remote, selection=Selection.load(path, default_feeds=["alpha", "beta"]))
    assert {"alpha/one.mp3", "beta/one.mp3", "beta/feed.xml"} <= set(remote.files)

    alpha, beta = scan(library)
    choices = Selection.load(path, default_feeds=["alpha", "beta"])
    choices.set(alpha, episodes={"one.mp3": False})
    choices.set(beta, published=False)
    choices.save()

    (published,) = sync(library, remote, selection=Selection.load(path))
    assert published.podcast.name == "alpha"
    assert set(remote.files) == {"alpha/two.mp3", "alpha/feed.xml"}
    assert sorted(remote.deleted) == ["alpha/one.mp3", "beta"]


def test_switching_everything_off_empties_dropbox(library, tmp_path):
    """Unlike a library that is missing, which leaves Dropbox alone."""
    remote = FakeRemote()
    path = tmp_path / "s.json"
    sync(library, remote, selection=Selection.load(path, default_feeds=["alpha", "beta"]))
    assert remote.files
    choices = Selection.load(path)
    for podcast in scan(library):
        choices.set(podcast, published=False)
    assert sync(library, remote, selection=choices) == []
    assert remote.files == {}


def test_feed_on_the_command_line_publishes_what_is_switched_off(library, tmp_path):
    choices = Selection.load(tmp_path / "s.json")
    choices.set(scan(library)[1], published=False, episodes={"two.mp3": False})
    (published,) = sync(library, FakeRemote(), names=["beta"], selection=choices)
    # Named, so published; but only its ticked episodes.
    assert [e.audio.name for e in published.podcast.episodes] == ["one.mp3"]


def test_what_is_published_is_adopted_and_new_podcasts_wait(library, tmp_path):
    path = tmp_path / "s.json"
    choices = Selection.load(path, default_feeds=["alpha"])
    assert choices.adopt(scan(library))
    assert json.loads(path.read_text())["podcasts"] == {
        "alpha": {"published": True}, "beta": {"published": False},
    }
    # Once only.
    assert not Selection.load(path).adopt(scan(library))

    gamma = library / "gamma"
    gamma.mkdir()
    _episode(gamma, "one", "2026-01-01T10:00:00+00:00")
    # The feeds option no longer matters: a new podcast is off until switched on.
    choices = Selection.load(path, default_feeds=None)
    assert list(_by_name(choices.choose(scan(library)))) == ["alpha"]


def test_an_empty_library_is_not_adopted(tmp_path):
    """Or every podcast would be new, and off, once the disk is back."""
    path = tmp_path / "s.json"
    assert not Selection.load(path).adopt([])
    assert not path.exists()


def test_sync_from_the_command_line_adopts_first(library, tmp_path, monkeypatch):
    from db_podcast import cli

    monkeypatch.setenv("DB_PODCAST_SELECTION", str(tmp_path / "s.json"))
    monkeypatch.setenv("DB_PODCAST_FEEDS", "alpha, beta")
    remote = FakeRemote()
    monkeypatch.setattr(cli, "_remote", lambda config: remote)
    assert cli.main(["--library", str(library), "sync"]) == 0
    assert {"alpha/feed.xml", "beta/feed.xml"} <= set(remote.files)

    gamma = library / "gamma"
    gamma.mkdir()
    _episode(gamma, "one", "2026-01-01T10:00:00+00:00")
    assert cli.main(["--library", str(library), "sync"]) == 0
    assert not any(name.startswith("gamma/") for name in remote.files)


def test_a_category_is_chosen_from_apples_list(library, tmp_path):
    from db_podcast.feed import DEFAULT_CATEGORY

    alpha = scan(library)[0]
    choices = Selection.load(tmp_path / "s.json")
    assert choices.category(alpha) == DEFAULT_CATEGORY
    choices.set(alpha, published=True, category="Kids & Family")
    assert choices.choose([alpha])[0].category == "Kids & Family"
    with pytest.raises(ValueError):
        choices.set(alpha, category="Cats")
