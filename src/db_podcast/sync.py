"""Mirror the library to Dropbox and publish a feed for each podcast.

The library is the truth and Dropbox follows it: files that are new or
changed are uploaded, and files that are no longer in the library are deleted.
Nothing is remembered between runs. What is already in Dropbox, and under
which URLs, is asked from Dropbox every time, so the library can move to
another machine without anything else having to move with it.

The library is only read. Each feed is built in a folder of its own and
uploaded from there — the library may well be fetchit's, and has no place in
it for a file of db-podcast's.
"""

import logging
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from db_podcast import feed
from db_podcast.library import FEED_FILE, Podcast, scan
from db_podcast.remote import content_hash

logger = logging.getLogger(__name__)

TIMEOUT = 30


class Remote(Protocol):
    """Where the library is mirrored to; see :class:`db_podcast.remote.DropboxRemote`."""

    def list(self) -> dict[str, str]: ...
    def upload(self, path: Path, name: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def url(self, name: str) -> str: ...


@dataclass(frozen=True)
class Published:
    """A podcast and the URL to subscribe to it with."""

    podcast: Podcast
    feed_url: str | None


def sync(
    library: str | Path,
    remote: Remote,
    prune: bool = True,
    dry_run: bool = False,
    names: list[str] | None = None,
    feed_dir: str | Path | None = None,
) -> list[Published]:
    """Bring ``remote`` in line with ``library`` and publish the feeds.

    Parameters
    ----------
    library
        A fetchit library, or a folder holding one sub-folder per podcast.
    remote
        The Dropbox folder to mirror it to.
    prune
        Delete remote files that are not (or no longer) in what is published.
    dry_run
        Only log what would be uploaded and deleted.
    names
        Only publish these podcasts (see :func:`db_podcast.library.scan`).
        Everything else in Dropbox is pruned like a podcast that is gone.
    feed_dir
        Where to write each feed before uploading it, one sub-folder per
        podcast. A temporary folder when not given: what Dropbox has is
        compared by content, so nothing local needs keeping.

    Returns
    -------
    list[Published]
        The podcasts with their feed URLs. The URLs are ``None`` in a dry run.
    """
    if feed_dir is None:
        with tempfile.TemporaryDirectory(prefix="db-podcast-") as work:
            return sync(library, remote, prune, dry_run, names, work)
    feed_dir = Path(feed_dir)

    podcasts = scan(library, names)
    # name as the remote compares it -> (name as the remote spells it, hash)
    remote_files = {_key(name): (name, hash_) for name, hash_ in remote.list().items()}
    mirror = _Mirror(remote, remote_files, dry_run)

    published = []
    for podcast in podcasts:
        names = {path: remote_name(podcast, path) for path in podcast.files}
        for path, name in names.items():
            mirror.push(path, name)
        feed_name = f"{podcast.name}/{FEED_FILE}"
        mirror.keep(feed_name)
        if dry_run:
            published.append(Published(podcast, None))
            continue

        urls = {path: remote.url(name) for path, name in names.items()}
        feed_path = feed_dir / podcast.name / FEED_FILE
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        if _key(feed_name) not in remote_files:
            # The feed names its own URL, and a file only gets a URL once it
            # is in Dropbox. So the very first time, it goes up without one.
            _write(feed_path, feed.build(podcast, urls))
            mirror.push(feed_path, feed_name)
        feed_url = remote.url(feed_name)
        _write(feed_path, feed.build(podcast, urls, feed_url))
        mirror.push(feed_path, feed_name)
        published.append(Published(podcast, feed_url))

    if prune:
        mirror.prune({_key(podcast.name) for podcast in podcasts})
    return published


def remote_name(podcast: Podcast, path: Path) -> str:
    """Where ``path`` goes in Dropbox: under the podcast's name, as it is filed.

    A series' episodes sit in season folders, and keep them; a flat folder's
    files are just their names.
    """
    return f"{podcast.name}/{path.relative_to(podcast.folder).as_posix()}"


def check(published: list[Published]) -> bool:
    """Fetch each feed as a podcast app would, and the first bytes of its files.

    Returns whether every feed and every file it links to could be fetched.
    """
    healthy = True
    for item in published:
        try:
            with _open(item.feed_url) as response:
                root = ET.parse(response).getroot()
        except (URLError, ET.ParseError) as error:
            logger.error("%s: cannot read the feed: %s", item.podcast.name, error)
            healthy = False
            continue
        enclosures = [element.get("url") for element in root.iter("enclosure")]
        logger.info("%s: feed is readable, %d episode(s)", item.podcast.name, len(enclosures))
        for url in enclosures:
            try:
                # One byte is enough to see that the file is there, and asking
                # for a range shows whether the app can seek in the episode.
                with _open(url, headers={"Range": "bytes=0-0"}) as response:
                    seekable = response.status == 206
                    media_type = response.headers.get_content_type()
            except URLError as error:
                logger.error("  cannot fetch %s: %s", url, error)
                healthy = False
                continue
            name = Path(unquote(urlsplit(url).path)).name
            note = "" if seekable else " (no range requests: plays, but cannot seek)"
            logger.info("  ok  %s  %s%s", name, media_type, note)
    return healthy


class _Mirror:
    """The uploads and deletions of one run, or in a dry run the log of them."""

    def __init__(self, remote: Remote, remote_files: dict[str, tuple[str, str]], dry_run: bool):
        self._remote = remote
        self._remote_files = remote_files
        self._dry_run = dry_run
        self._kept: set[str] = set()

    def keep(self, name: str) -> None:
        """Mark ``name`` as belonging to the library, so it is not pruned."""
        self._kept.add(_key(name))

    def push(self, path: Path, name: str) -> None:
        """Upload ``path`` as ``name`` unless the remote has that content already."""
        self.keep(name)
        hash_ = content_hash(path)
        if self._remote_files.get(_key(name), (None, None))[1] == hash_:
            return
        logger.info("%s %s", "Would upload" if self._dry_run else "Uploading", name)
        if not self._dry_run:
            self._remote.upload(path, name)
            self._remote_files[_key(name)] = (name, hash_)

    def prune(self, podcasts: set[str]) -> None:
        """Delete what is on the remote without being in the library."""
        stale = [name for key, (name, _) in self._remote_files.items() if key not in self._kept]
        if not stale:
            return
        if not podcasts:
            # An empty library is far more likely to be a disk that is not
            # mounted than a wish to delete every podcast, and the feed URLs
            # that people subscribed to would be gone with them.
            logger.warning(
                "The library has no podcasts; leaving %d file(s) in Dropbox alone.", len(stale)
            )
            return
        targets = []
        for name in stale:
            folder = name.split("/")[0]
            # A podcast that is gone is deleted as a folder, not file by file.
            target = name if _key(folder) in podcasts else folder
            if target not in targets:
                targets.append(target)
        for target in targets:
            logger.info("%s %s", "Would delete" if self._dry_run else "Deleting", target)
            if not self._dry_run:
                self._remote.delete(target)


def _key(name: str) -> str:
    """Return ``name`` as Dropbox compares names: ignoring case and Unicode form."""
    return unicodedata.normalize("NFC", name).lower()


def _write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path``, unless that is what it holds already."""
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def _open(url: str, headers: dict[str, str] | None = None):
    request = Request(url, headers={"User-Agent": "db-podcast", **(headers or {})})
    return urlopen(request, timeout=TIMEOUT)
