"""Build the RSS feed a podcast app subscribes to."""

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from email.utils import format_datetime
from pathlib import Path

from db_podcast.library import Podcast

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"

ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def build(podcast: Podcast, urls: Mapping[Path, str], feed_url: str | None = None) -> bytes:
    """Return the feed of ``podcast`` as XML.

    The result depends only on the arguments (it carries no "generated at"
    time), so an unchanged podcast gives a byte-for-byte unchanged feed and
    nothing has to be uploaded again.

    Parameters
    ----------
    podcast
        The podcast, as read from the library.
    urls
        The public URL of each file in ``podcast.files``.
    feed_url
        The public URL of the feed itself, once it has one.

    Returns
    -------
    bytes
        The UTF-8 encoded XML document.
    """
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    _add(channel, "title", podcast.title)
    link = podcast.link or feed_url
    if link:
        _add(channel, "link", link)
    _add(channel, "description", podcast.description)
    _add(channel, "language", podcast.language)
    if podcast.episodes:
        _add(channel, "lastBuildDate", format_datetime(podcast.episodes[0].published))
    if feed_url:
        ET.SubElement(
            channel, _atom("link"), href=feed_url, rel="self", type="application/rss+xml"
        )
    _add(channel, _itunes("author"), podcast.author)
    _add(channel, _itunes("explicit"), "true" if podcast.explicit else "false")
    # A feed for the household: keep it out of the podcast directories.
    _add(channel, _itunes("block"), "yes")
    if podcast.cover:
        ET.SubElement(channel, _itunes("image"), href=urls[podcast.cover])

    for episode in podcast.episodes:
        item = ET.SubElement(channel, "item")
        _add(item, "title", episode.title)
        _add(item, "description", episode.description)
        # The file's place in the library, not its URL: Dropbox may hand out a
        # new URL one day, and that should not make every episode look new.
        guid = _add(item, "guid", f"{podcast.name}/{episode.audio.name}")
        guid.set("isPermaLink", "false")
        _add(item, "pubDate", format_datetime(episode.published))
        ET.SubElement(
            item,
            "enclosure",
            url=urls[episode.audio],
            length=str(episode.audio.stat().st_size),
            type=episode.mime_type,
        )
        if episode.duration is not None:
            _add(item, _itunes("duration"), str(episode.duration))
        if episode.number is not None:
            _add(item, _itunes("episode"), str(episode.number))
        if episode.cover:
            ET.SubElement(item, _itunes("image"), href=urls[episode.cover])

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def _add(parent: ET.Element, tag: str, text: str) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _itunes(tag: str) -> str:
    return f"{{{ITUNES}}}{tag}"


def _atom(tag: str) -> str:
    return f"{{{ATOM}}}{tag}"
