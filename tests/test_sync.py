"""Tests for the sync, against a stand-in for Dropbox."""

import json
import xml.etree.ElementTree as ET

import pytest

from db_podcast.remote import content_hash
from db_podcast.sync import sync


class FakeRemote:
    """Keeps the uploads in memory, and hands out URLs the way Dropbox does:
    one per file, unrelated to its name, and stable for as long as it exists."""

    def __init__(self):
        self.files = {}
        self.links = {}
        self.uploads = []
        self.deleted = []

    def list(self):
        return dict(self.files)

    def upload(self, path, name):
        self.files[name] = content_hash(path)
        self.uploads.append(name)

    def delete(self, name):
        self.deleted.append(name)
        for existing in list(self.files):
            if existing == name or existing.startswith(name + "/"):
                del self.files[existing]
                self.links.pop(existing.lower(), None)

    def url(self, name, download=False):
        key = name.lower()
        assert key in {existing.lower() for existing in self.files}, (
            f"{name} was shared before it was uploaded"
        )
        self.links.setdefault(key, f"https://dropbox.test/{len(self.links)}/{key}?raw=1")
        return self.links[key].replace("raw=1", "dl=1") if download else self.links[key]


@pytest.fixture
def library(tmp_path):
    """A library with one podcast of two episodes, one of which has artwork."""
    show = tmp_path / "library" / "show"
    show.mkdir(parents=True)
    (show / "first.mp3").write_bytes(b"first")
    (show / "first.png").write_bytes(b"first cover")
    (show / "first.json").write_text(
        json.dumps({"title": "The first", "published": "2026-01-01T10:00:00+00:00", "number": 7})
    )
    (show / "second.mp3").write_bytes(b"second")
    (show / "second.json").write_text(json.dumps({"published": "2026-02-01T10:00:00+00:00"}))
    return tmp_path / "library"


def test_sync_uploads_files_and_feed(library):
    """Test that the audio, the artwork and a feed linking to them are uploaded."""
    remote = FakeRemote()
    feeds = library.parent / "feeds"
    (published,) = sync(library, remote, feed_dir=feeds)

    assert set(remote.files) == {
        "show/first.mp3",
        "show/first.png",
        "show/second.mp3",
        "show/feed.xml",
    }
    assert published.feed_url == remote.links["show/feed.xml"]

    # Built beside the library, not in it: the library may well be fetchit's.
    assert not (library / "show" / "feed.xml").exists()
    channel = ET.parse(feeds / "show" / "feed.xml").getroot().find("channel")
    items = channel.findall("item")
    # Newest first.
    assert [item.findtext("title") for item in items] == ["second", "The first"]
    assert [item.find("enclosure").get("url") for item in items] == [
        remote.url("show/second.mp3", download=True),
        remote.url("show/first.mp3", download=True),
    ]
    # Episodes are linked to as downloads; the feed and the artwork are not.
    assert all(item.find("enclosure").get("url").endswith("dl=1") for item in items)
    assert published.feed_url.endswith("raw=1")
    itunes_episode = "{http://www.itunes.com/dtds/podcast-1.0.dtd}episode"
    assert [item.findtext(itunes_episode) for item in items] == [None, "7"]
    assert [item.findtext("pubDate") for item in items] == [
        "Sun, 01 Feb 2026 10:00:00 +0000",
        "Thu, 01 Jan 2026 10:00:00 +0000",
    ]
    atom_link = channel.find("{http://www.w3.org/2005/Atom}link")
    assert atom_link.get("href") == published.feed_url
    # The metadata stays at home; only what the feed links to goes up.
    assert not any(name.endswith(".json") for name in remote.files)


def test_sync_is_idempotent(library):
    """Test that a second run uploads nothing and keeps the feed URL."""
    remote = FakeRemote()
    (first,) = sync(library, remote)
    remote.uploads.clear()

    (second,) = sync(library, remote)
    assert remote.uploads == []
    assert remote.deleted == []
    assert second.feed_url == first.feed_url


def test_sync_uploads_new_episode_and_feed_only(library):
    """Test that adding an episode uploads it and the feed, and nothing else."""
    remote = FakeRemote()
    sync(library, remote)
    remote.uploads.clear()

    (library / "show" / "third.mp3").write_bytes(b"third")
    sync(library, remote)
    assert remote.uploads == ["show/third.mp3", "show/feed.xml"]


def test_sync_prunes_removed_files(library):
    """Test that an episode removed from the library is deleted remotely."""
    remote = FakeRemote()
    sync(library, remote)

    (library / "show" / "first.mp3").unlink()
    (library / "show" / "first.png").unlink()
    sync(library, remote)
    assert sorted(remote.deleted) == ["show/first.mp3", "show/first.png"]
    assert set(remote.files) == {"show/second.mp3", "show/feed.xml"}


def test_sync_prunes_removed_podcast_as_folder(library):
    """Test that a podcast removed from the library is deleted as one folder."""
    other = library / "other"
    other.mkdir()
    (other / "only.mp3").write_bytes(b"only")
    remote = FakeRemote()
    sync(library, remote)

    (other / "only.mp3").unlink()
    sync(library, remote)
    assert remote.deleted == ["other"]
    assert not any(name.startswith("other/") for name in remote.files)


def test_sync_keeps_remote_when_library_is_empty(library):
    """Test that an empty library (an unmounted disk) deletes nothing."""
    remote = FakeRemote()
    sync(library, remote)
    before = dict(remote.files)

    for path in (library / "show").iterdir():
        path.unlink()
    assert sync(library, remote) == []
    assert remote.deleted == []
    assert remote.files == before


def test_sync_no_prune(library):
    """Test that nothing is deleted when pruning is turned off."""
    remote = FakeRemote()
    sync(library, remote)

    (library / "show" / "first.mp3").unlink()
    sync(library, remote, prune=False)
    assert remote.deleted == []


def test_sync_dry_run_changes_nothing(library):
    """Test that a dry run uploads, shares, deletes and writes nothing."""
    remote = FakeRemote()
    remote.files["gone/old.mp3"] = "0" * 64

    (published,) = sync(library, remote, dry_run=True)
    assert published.feed_url is None
    assert remote.uploads == []
    assert remote.links == {}
    assert remote.deleted == []
    assert not (library / "show" / "feed.xml").exists()


def test_sync_matches_remote_names_ignoring_case(library):
    """Test that a file Dropbox spells with other capitals is not sent again."""
    remote = FakeRemote()
    sync(library, remote)
    remote.files["Show/First.mp3"] = remote.files.pop("show/first.mp3")
    remote.uploads.clear()

    sync(library, remote, prune=False)
    assert "show/first.mp3" not in remote.uploads


@pytest.fixture
def fetchit_library(tmp_path):
    """A fetchit library: a podcast, a series with seasons, and articles."""
    root = tmp_path / "fetchit"
    (root / ".fetchit").mkdir(parents=True)
    pod = root / "podcasts" / "Bureau Buitenland"
    pod.mkdir(parents=True)
    (pod / "feed.json").write_text(json.dumps({"name": "Bureau Buitenland", "title": "Bureau Buitenland", "language": "nl"}))
    (pod / "cover.jpg").write_bytes(b"cover")
    (pod / "2026-10-19 - Europa.mp3").write_bytes(b"ep")
    (pod / "2026-10-19 - Europa.json").write_text(json.dumps({"id": "x", "title": "Europa", "published": "2026-10-19T12:24:00+00:00"}))
    season = root / "shows" / "Karambolage" / "Season 2026"
    season.mkdir(parents=True)
    (season.parent / "feed.json").write_text(json.dumps({"name": "Karambolage", "image": "poster.jpg"}))
    (season.parent / "poster.jpg").write_bytes(b"poster")
    stem = "Karambolage - S2026E101801 - Les feuilles"
    (season / f"{stem}.mp4").write_bytes(b"video")
    (season / f"{stem}-thumb.jpg").write_bytes(b"thumb")
    (season / f"{stem}.json").write_text(json.dumps({"id": "y", "title": "Les feuilles", "published": "2026-10-18T03:00:00+00:00", "image": f"{stem}-thumb.jpg"}))
    articles = root / "articles" / "Blog"
    articles.mkdir(parents=True)
    (articles / "2026-10-01 - Post.json").write_text(json.dumps({"id": "z", "title": "Post"}))
    return root


def test_sync_publishes_only_the_feeds_it_is_asked_for(fetchit_library):
    remote = FakeRemote()
    (published,) = sync(fetchit_library, remote, names=["bureau buitenland"])
    assert published.podcast.title == "Bureau Buitenland"
    assert set(remote.files) == {
        "Bureau Buitenland/cover.jpg",
        "Bureau Buitenland/2026-10-19 - Europa.mp3",
        "Bureau Buitenland/feed.xml",
    }


def test_sync_keeps_a_series_season_folders(fetchit_library):
    remote = FakeRemote()
    sync(fetchit_library, remote, names=["Karambolage"])
    stem = "Karambolage - S2026E101801 - Les feuilles"
    assert set(remote.files) == {
        "Karambolage/poster.jpg",
        f"Karambolage/Season 2026/{stem}.mp4",
        f"Karambolage/Season 2026/{stem}-thumb.jpg",
        "Karambolage/feed.xml",
    }


def test_sync_prunes_a_feed_that_is_no_longer_asked_for(fetchit_library):
    remote = FakeRemote()
    sync(fetchit_library, remote)
    sync(fetchit_library, remote, names=["Karambolage"])
    assert remote.deleted == ["Bureau Buitenland"]


def test_the_feed_has_what_validators_ask_for(library):
    """Owner, category, type, a plain RSS image, and the podcast namespace."""
    (library / "show" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0 a jpeg")
    feeds = library.parent / "feeds"
    (published,) = sync(library, FakeRemote(), feed_dir=feeds, owner_email="me@example.org")
    channel = ET.parse(feeds / "show" / "feed.xml").getroot().find("channel")
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    pi = "{https://podcastindex.org/namespace/1.0}"

    assert channel.find(itunes + "category").get("text") == "Leisure"
    assert channel.findtext(f"{itunes}owner/{itunes}email") == "me@example.org"
    assert channel.findtext(f"{itunes}owner/{itunes}name") == "show"
    assert channel.findtext(itunes + "type") == "episodic"
    assert channel.findtext("image/url") == channel.find(itunes + "image").get("href")
    assert channel.findtext("image/title") == "show"
    assert channel.find(pi + "locked").text == "yes"
    assert channel.find(pi + "locked").get("owner") == "me@example.org"
    # A UUIDv5 of the feed's URL, the same every time.
    from db_podcast.feed import feed_guid
    assert channel.findtext(pi + "guid") == str(feed_guid(published.feed_url))
    assert [i.findtext(itunes + "episodeType") for i in channel.findall("item")] == ["full", "full"]


def test_no_owner_email_unless_one_is_given(library):
    feeds = library.parent / "feeds"
    sync(library, FakeRemote(), feed_dir=feeds)
    channel = ET.parse(feeds / "show" / "feed.xml").getroot().find("channel")
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    assert channel.find(f"{itunes}owner/{itunes}email") is None
    assert channel.find("{https://podcastindex.org/namespace/1.0}locked").get("owner") is None


def test_an_image_goes_up_under_the_suffix_of_what_it_is(library):
    """fetchit calls a feed's artwork cover.jpg, PNG or not."""
    (library / "show" / "cover.jpg").write_bytes(b"\x89PNG\r\n\x1a\n a png")
    (library / "show" / "first.png").write_bytes(b"\xff\xd8\xff\xe0 really a jpeg")
    remote = FakeRemote()
    sync(library, remote)
    assert {"show/cover.png", "show/first.jpg"} <= set(remote.files)
    assert not {"show/cover.jpg", "show/first.png"} & set(remote.files)


def _image(path, size, format):
    from PIL import Image

    Image.new("RGB", size, (200, 30, 30)).save(path, format=format)


@pytest.mark.parametrize("format, suffix", [("JPEG", ".jpg"), ("PNG", ".png")])
def test_small_cover_is_scaled_up_once(library, tmp_path, format, suffix):
    """Apple wants 1400 pixels: a smaller cover goes up scaled, the library keeps its own."""
    from PIL import Image

    cover = library / "show" / f"cover{suffix}"
    _image(cover, (516, 516), format)
    original = cover.read_bytes()
    remote = FakeRemote()
    work = tmp_path / "work"

    sync(library, remote, feed_dir=work)

    name = f"show/cover{suffix}"
    assert name in remote.files
    scaled = work / "show" / f"cover{suffix}"
    with Image.open(scaled) as image:
        assert image.size == (1400, 1400)
        assert image.format == format
    assert remote.files[name] == content_hash(scaled)
    assert cover.read_bytes() == original

    remote.uploads.clear()
    sync(library, remote, feed_dir=tmp_path / "other")
    assert name not in remote.uploads


def test_cover_keeps_its_shape(library, tmp_path):
    _image(library / "show" / "cover.png", (600, 300), "PNG")
    from PIL import Image

    sync(library, FakeRemote(), feed_dir=tmp_path / "work")
    with Image.open(tmp_path / "work" / "show" / "cover.png") as image:
        assert image.size == (2800, 1400)


def test_large_cover_goes_up_as_it_is(library, tmp_path):
    cover = library / "show" / "cover.png"
    _image(cover, (1400, 1400), "PNG")
    remote = FakeRemote()

    sync(library, remote, feed_dir=tmp_path / "work")

    assert remote.files["show/cover.png"] == content_hash(cover)
    assert not (tmp_path / "work" / "show" / "cover.png").exists()


def test_unreadable_cover_goes_up_as_it_is(library, tmp_path):
    cover = library / "show" / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n not really")
    remote = FakeRemote()

    sync(library, remote, feed_dir=tmp_path / "work")

    assert remote.files["show/cover.png"] == content_hash(cover)
