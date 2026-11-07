"""Tests for reading the library folder."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from db_podcast import library
from db_podcast.library import LibraryError


def test_scan_needs_no_metadata(tmp_path):
    """Test that a folder with an audio file is a podcast as it is."""
    (tmp_path / "My Show").mkdir()
    (tmp_path / "My Show" / "Episode one.mp3").write_bytes(b"not really audio")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.mp3").write_bytes(b"x")

    (podcast,) = library.scan(tmp_path)
    assert podcast.title == "My Show"
    assert podcast.language == "en"
    (episode,) = podcast.episodes
    assert episode.title == "Episode one"
    assert episode.duration is None
    assert episode.mime_type == "audio/mpeg"
    assert episode.published.tzinfo is not None


def test_scan_reads_metadata_and_artwork(tmp_path):
    """Test that the JSON files and same-named images are picked up."""
    show = tmp_path / "show"
    show.mkdir()
    (show / "podcast.json").write_text(
        json.dumps({"title": "Le Show", "language": "fr", "author": "Moi"}), encoding="utf-8"
    )
    (show / "a.mp3").write_bytes(b"a")
    (show / "a.PNG").write_bytes(b"cover of a")
    (show / "a.json").write_text(
        json.dumps({"title": "Épisode A", "published": "2026-03-01T08:00:00+01:00"}),
        encoding="utf-8",
    )
    (show / "b.mp3").write_bytes(b"b")
    (show / "b.json").write_text(json.dumps({"published": "2026-01-01"}))
    (show / "feed.xml").write_text("<rss/>")

    (podcast,) = library.scan(tmp_path)
    assert (podcast.title, podcast.language, podcast.author) == ("Le Show", "fr", "Moi")
    newest, oldest = podcast.episodes
    assert newest.title == "Épisode A"
    assert newest.cover == show / "a.PNG"
    # A date without a time zone is taken as UTC.
    assert oldest.published == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Without a cover of its own, the podcast borrows the newest episode's.
    assert podcast.cover == show / "a.PNG"
    assert podcast.files == [show / "a.PNG", show / "a.mp3", show / "b.mp3"]


def test_scan_prefers_podcast_cover(tmp_path):
    """Test that ``cover.*`` is the podcast's artwork and not an episode's."""
    show = tmp_path / "show"
    show.mkdir()
    (show / "cover.jpg").write_bytes(b"cover")
    (show / "a.mp3").write_bytes(b"a")
    (show / "a.png").write_bytes(b"cover of a")

    (podcast,) = library.scan(tmp_path)
    assert podcast.cover == show / "cover.jpg"
    assert len(podcast.files) == 3


def test_scan_rejects_broken_metadata(tmp_path):
    """Test that a typo in a JSON file is reported with the file's name."""
    show = tmp_path / "show"
    show.mkdir()
    (show / "a.mp3").write_bytes(b"a")
    (show / "a.json").write_text("{not json")
    with pytest.raises(LibraryError, match="a.json"):
        library.scan(tmp_path)


def test_scan_missing_library(tmp_path):
    """Test that a library folder that is not there is an error, not empty."""
    with pytest.raises(LibraryError):
        library.scan(tmp_path / "nope")


@pytest.mark.parametrize("number", ["288", 2.5, True])
def test_scan_rejects_a_number_that_is_not_whole(tmp_path, number):
    """Test that a number that cannot go into the feed is reported."""
    show = tmp_path / "show"
    show.mkdir()
    (show / "a.mp3").write_bytes(b"a")
    (show / "a.json").write_text(json.dumps({"number": number}))
    with pytest.raises(LibraryError, match="number"):
        library.scan(tmp_path)




# --- a fetchit library ------------------------------------------------------


def _fetchit(tmp_path):
    root = tmp_path / "fetchit"
    (root / ".fetchit").mkdir(parents=True)
    return root


def _feed(root, kind, name, **meta):
    folder = root / kind / name
    folder.mkdir(parents=True)
    (folder / "feed.json").write_text(json.dumps({"name": name, **meta}), encoding="utf-8")
    return folder


def test_scan_reads_the_podcasts_and_shows_of_a_fetchit_library(tmp_path):
    root = _fetchit(tmp_path)
    pod = _feed(root, "podcasts", "Pod", title="The Pod", language="nl", author="NPO")
    (pod / "2026-10-19 - Ep.mp3").write_bytes(b"a")
    show = _feed(root, "shows", "Show", image="poster.jpg")
    (show / "poster.jpg").write_bytes(b"poster")
    (show / "Season 2026").mkdir()
    (show / "Season 2026" / "Show - S2026E101901 - T.mp4").write_bytes(b"v")
    blog = _feed(root, "articles", "Blog")
    (blog / "2026-10-19 - Post.json").write_text("{}")

    pod_, show_ = library.scan(root)

    assert (pod_.name, pod_.title, pod_.language, pod_.author) == ("Pod", "The Pod", "nl", "NPO")
    assert show_.name == "Show"
    assert show_.cover == show / "poster.jpg"
    (episode,) = show_.episodes
    assert episode.mime_type == "video/mp4"
    assert episode.audio.parent.name == "Season 2026"


def test_scan_takes_an_episodes_picture_from_its_record_or_its_thumb(tmp_path):
    root = _fetchit(tmp_path)
    season = _feed(root, "shows", "Show") / "Season 02"
    season.mkdir()
    (season / "a.mp4").write_bytes(b"v")
    (season / "a-thumb.jpg").write_bytes(b"t")
    (season / "b.mp4").write_bytes(b"v")
    (season / "b-still.png").write_bytes(b"s")
    (season / "b.json").write_text(json.dumps({"image": "b-still.png"}))

    (show,) = library.scan(root)
    covers = {episode.audio.name: episode.cover.name for episode in show.episodes}
    assert covers == {"a.mp4": "a-thumb.jpg", "b.mp4": "b-still.png"}


def test_scan_tells_a_feeds_audio_and_video_apart_in_dropbox(tmp_path):
    root = _fetchit(tmp_path)
    (_feed(root, "podcasts", "Both") / "a.mp3").write_bytes(b"a")
    (_feed(root, "shows", "Both") / "a.mp4").write_bytes(b"v")
    assert [p.name for p in library.scan(root)] == ["Both", "Both (shows)"]


def test_scan_selects_podcasts_by_name_or_title_ignoring_case(tmp_path):
    root = _fetchit(tmp_path)
    (_feed(root, "podcasts", "mbh", title="Mes premières Belles Histoires") / "a.mp3").write_bytes(b"a")
    (_feed(root, "podcasts", "Other") / "a.mp3").write_bytes(b"a")

    assert [p.name for p in library.scan(root, ["MBH"])] == ["mbh"]
    assert [p.name for p in library.scan(root, ["mes premières belles histoires"])] == ["mbh"]


def test_scan_says_which_asked_for_podcast_is_not_there(tmp_path):
    root = _fetchit(tmp_path)
    (_feed(root, "podcasts", "Pod") / "a.mp3").write_bytes(b"a")
    with pytest.raises(LibraryError, match="'nope'"):
        library.scan(root, ["Pod", "Nope"])


def test_scan_of_a_hand_made_library_still_reads_feed_json(tmp_path):
    show = tmp_path / "show"
    show.mkdir()
    (show / "feed.json").write_text(json.dumps({"title": "From fetchit"}))
    (show / "a.mp3").write_bytes(b"a")
    (podcast,) = library.scan(tmp_path)
    assert podcast.title == "From fetchit"
