"""The local folder that defines the podcasts and their episodes.

Two layouts are read. The usual one is the library `fetchit
<https://github.com/bertcoerver/fetchit>`_ fills, where every feed it downloads
has a folder of its own and a podcast is any of those with media in it::

    <library>/                      a fetchit library (it has a .fetchit folder)
        podcasts/<Feed>/            audio
            feed.json               what the feed says about itself
            cover.jpg
            2026-10-19 - Title.mp3  an episode
            2026-10-19 - Title.jpg  its artwork
            2026-10-19 - Title.json its title, description, published, number
        shows/<Feed>/               video, one Season folder per year or season
            feed.json  poster.jpg
            Season 2026/<…>.mp4     + <…>-thumb.jpg, <…>.json

The other is a folder made by hand, one sub-folder per podcast::

    <library>/
        my-show/
            podcast.json            optional: title, description, author, ...
            cover.png               optional: the podcast's artwork
            un-petit-air.mp3        an episode
            un-petit-air.png        optional: its artwork (same name)
            un-petit-air.json       optional: its title, description, published, number

Everything optional has a fallback, so a folder with a few mp3 files in it is
already a podcast: titles come from the audio tags or the file names, and
dates from the files' modification times. Either way the library is only ever
read: the feed that is published is built elsewhere (see
:func:`db_podcast.sync.sync`), so a library fetchit owns stays its own.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import mutagen

# Suffix -> the MIME type the feed announces for it. Video goes into a feed as
# readily as audio, and podcast apps play it.
MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/x-m4a",
    ".m4b": "audio/x-m4a",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")

PODCAST_FILE = "podcast.json"
# What fetchit calls the same thing, with the same keys.
FETCHIT_FEED_FILE = "feed.json"
FEED_FILE = "feed.xml"
COVER_NAME = "cover"
THUMB_SUFFIX = "-thumb"

# A fetchit library marks itself with this folder, and files the feeds that can
# be podcasts under these two.
FETCHIT_DIR = ".fetchit"
FETCHIT_KINDS = ("podcasts", "shows")


class LibraryError(Exception):
    """Raised when the library folder cannot be read as podcasts."""


@dataclass(frozen=True)
class Episode:
    """One audio file of a podcast."""

    audio: Path
    title: str
    description: str
    published: datetime
    number: int | None
    duration: int | None
    cover: Path | None

    @property
    def mime_type(self) -> str:
        return MEDIA_TYPES[self.audio.suffix.lower()]


@dataclass(frozen=True)
class Podcast:
    """One folder of the library."""

    folder: Path
    name: str
    title: str
    description: str
    author: str
    language: str
    link: str | None
    explicit: bool
    cover: Path | None
    episodes: list[Episode] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        """The files the feed links to: the audio and the artwork."""
        files = [self.cover] if self.cover else []
        for episode in self.episodes:
            files.append(episode.audio)
            if episode.cover:
                files.append(episode.cover)
        # An episode's artwork can double as the podcast's.
        return list(dict.fromkeys(files))


def scan(library: str | Path, names: list[str] | None = None) -> list[Podcast]:
    """Read the podcasts in ``library``.

    Parameters
    ----------
    library
        A fetchit library, or a folder holding one sub-folder per podcast.
    names
        Only these podcasts, by folder name or title, ignoring case. All of
        them when not given.

    Returns
    -------
    list[Podcast]
        The podcasts, by name. Folders without media are skipped.
    """
    library = Path(library)
    if not library.is_dir():
        raise LibraryError(f"Library folder does not exist: {library}")
    wanted = {name.strip().casefold() for name in names} if names else None

    podcasts = []
    taken: set[str] = set()
    for folder in _podcast_folders(library):
        podcast = _read_podcast(folder, _unique(folder, taken))
        if podcast.episodes:
            taken.add(podcast.name.casefold())
            podcasts.append(podcast)

    if wanted is not None:
        podcasts = [p for p in podcasts if wanted & _names(p)]

    if wanted is not None:
        found = set().union(*(_names(p) for p in podcasts))
        missing = sorted(wanted - found)
        if missing:
            raise LibraryError(
                f"No podcast called {', '.join(repr(m) for m in missing)} in {library} "
                "(or nothing to play in it yet)."
            )
    return podcasts


def _names(podcast: Podcast) -> set[str]:
    """What a podcast can be asked for by: its folder, its name, its title."""
    return {podcast.folder.name.casefold(), podcast.name.casefold(), podcast.title.casefold()}


def _podcast_folders(library: Path) -> list[Path]:
    """The folders of ``library`` that may be podcasts, in order."""
    if (library / FETCHIT_DIR).is_dir():
        found = []
        for kind in FETCHIT_KINDS:
            base = library / kind
            if base.is_dir():
                found.extend(_subfolders(base))
        return found
    return _subfolders(library)


def _subfolders(base: Path) -> list[Path]:
    return [f for f in sorted(base.iterdir()) if f.is_dir() and not f.name.startswith(".")]


def _unique(folder: Path, taken: set[str]) -> str:
    """The podcast's name in Dropbox: its folder's, unless another podcast has it.

    A fetchit feed with both audio and video has a folder of each kind, under
    the same name; the second one is told apart by its kind.
    """
    name = folder.name
    if name.casefold() in taken:
        name = f"{name} ({folder.parent.name})"
    return name


def _read_podcast(folder: Path, name: str) -> Podcast:
    metadata = _read_json(folder / PODCAST_FILE) or _read_json(folder / FETCHIT_FEED_FILE)
    top = {path.name.lower(): path for path in folder.iterdir() if path.is_file()}

    episodes = [_read_episode(path) for path in _media(folder)]
    # Newest first, as podcast apps expect; the name settles a tie.
    episodes.sort(key=lambda episode: episode.audio.name)
    episodes.sort(key=lambda episode: episode.published, reverse=True)

    cover = _find_image(COVER_NAME, top)
    if cover is None and isinstance(metadata.get("image"), str):
        named = folder / Path(metadata["image"]).name
        cover = named if named.is_file() else None
    if cover is None:
        # No artwork of its own: borrow the newest episode's.
        cover = next((episode.cover for episode in episodes if episode.cover), None)

    title = metadata.get("title") or folder.name
    return Podcast(
        folder=folder,
        name=name,
        title=title,
        description=metadata.get("description") or title,
        author=metadata.get("author") or title,
        language=metadata.get("language") or "en",
        link=metadata.get("link") or None,
        explicit=bool(metadata.get("explicit", False)),
        cover=cover,
        episodes=episodes,
    )


def _media(folder: Path) -> list[Path]:
    """The episodes' files: in the folder, or in folders below it (a series' seasons)."""
    found = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in MEDIA_TYPES:
            found.append(path)
    return found


def _read_episode(audio: Path) -> Episode:
    metadata = _read_json(audio.with_suffix(".json"))
    tag_title, duration = _read_tags(audio)
    title = metadata.get("title") or tag_title or audio.stem
    return Episode(
        audio=audio,
        title=title,
        description=metadata.get("description") or title,
        published=_published(metadata.get("published"), audio),
        number=_number(metadata.get("number"), audio),
        duration=duration,
        cover=_episode_image(audio, metadata),
    )


def _episode_image(audio: Path, metadata: dict) -> Path | None:
    """An episode's artwork: the one its record names, or one under its own name."""
    named = metadata.get("image")
    if isinstance(named, str):
        path = audio.parent / Path(named).name
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            return path
    files = {path.name.lower(): path for path in audio.parent.iterdir() if path.is_file()}
    stem = audio.stem.lower()
    return _find_image(stem, files) or _find_image(stem + THUMB_SUFFIX, files)


def _read_tags(audio: Path) -> tuple[str | None, int | None]:
    """Return the title and the duration in seconds stored in an audio file."""
    try:
        tags = mutagen.File(audio, easy=True)
    except mutagen.MutagenError:
        tags = None
    if tags is None:
        return None, None
    title = (tags.get("title") or [None])[0]
    return title, round(tags.info.length)


def _published(value: str | None, audio: Path) -> datetime:
    if value:
        try:
            published = datetime.fromisoformat(value)
        except ValueError as error:
            raise LibraryError(
                f"{audio.with_suffix('.json')}: 'published' is not an ISO date: {value}"
            ) from error
    else:
        published = datetime.fromtimestamp(audio.stat().st_mtime, timezone.utc)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    # Whole seconds, which is all a feed can express.
    return published.replace(microsecond=0)


def _number(value, audio: Path) -> int | None:
    # bool is an int to Python, but `true` is not a number to anyone else.
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise LibraryError(
            f"{audio.with_suffix('.json')}: 'number' is not a whole number: {value!r}"
        )
    return value


def _find_image(stem: str, files: dict[str, Path]) -> Path | None:
    for suffix in IMAGE_SUFFIXES:
        if stem + suffix in files:
            return files[stem + suffix]
    return None


def _read_json(path: Path) -> dict:
    """Return the object in a JSON file, or an empty one if there is no file."""
    try:
        with path.open(encoding="utf-8") as file:
            metadata = json.load(file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise LibraryError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(metadata, dict):
        raise LibraryError(f"{path} should hold a JSON object")
    return metadata
