"""Which podcasts are published, and which of their episodes.

The GUI writes these choices to a JSON file of db-podcast's own, never into the
library (which is fetchit's, and read only)::

    {
      "podcasts": {
        "Bureau Buitenland": {
          "published": true,
          "auto": true,
          "episodes": {"2026-11-06 - Ep.mp3": true, "2026-10-30 - Old.mp3": false}
        }
      }
    }

Podcasts are keyed by their name in Dropbox (:attr:`Podcast.name`), episodes by
their path inside the podcast's folder. An episode with no choice of its own
follows the podcast's ``auto``: with it on, an episode is published as soon as
fetchit has it; with it off, it waits to be ticked.

A podcast with no entry is one that is new since the file was made, and is
not published until it is switched on. Before there is a file at all, the
podcasts follow ``DB_PODCAST_FEEDS`` (every podcast, when that is empty), as
they did before there were choices; :meth:`Selection.adopt` writes that down
the first time the library has podcasts in it, so nothing published so far
is taken down by the switch.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from db_podcast.feed import CATEGORIES, DEFAULT_CATEGORY
from db_podcast.library import Episode, LibraryError, Podcast, aliases


class Selection:
    """The choices in one file, with the fallback for podcasts not in it."""

    def __init__(self, data: dict | None = None, default_feeds: list[str] | None = None,
                 path: Path | None = None, exists: bool = False):
        self.podcasts: dict[str, dict] = dict((data or {}).get("podcasts") or {})
        self.exists = exists
        """Whether the choices were made: a file, as opposed to none yet."""
        self.default_feeds = (
            {name.strip().casefold() for name in default_feeds} if default_feeds else None
        )
        self.path = path

    @classmethod
    def load(cls, path: str | Path, default_feeds: list[str] | None = None) -> Selection:
        """Read the choices in ``path``; a missing file is no choices at all.

        A file that cannot be read is an error rather than no choices: taken
        as none, it would publish what was switched off.
        """
        path = Path(path).expanduser()
        exists = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data, exists = {}, False
        except (OSError, ValueError) as error:
            raise LibraryError(f"Cannot read the choices in {path}: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("podcasts", {}), dict):
            raise LibraryError(f"{path} does not hold db-podcast's choices.")
        return cls(data, default_feeds, path, exists)

    def save(self) -> None:
        """Write the choices back, in one go so a sync never reads half a file."""
        if self.path is None:
            raise ValueError("This selection has no file to be saved to.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"podcasts": self.podcasts}, ensure_ascii=False, indent=1, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        self.exists = True

    def adopt(self, podcasts: list[Podcast]) -> bool:
        """Write down what is published now, the first time there is anything.

        Until then no podcast is new; from then on, one without an entry is.
        An empty library is not adopted: it is far more likely to be a disk
        that is not there than a library without podcasts, and adopting it
        would make every podcast new — switched off — once the disk is back.

        Returns whether the choices were saved.
        """
        if self.exists or not podcasts:
            return False
        for podcast in podcasts:
            entry = dict(self._entry(podcast))
            entry.setdefault("published", self.published(podcast))
            self.podcasts[podcast.name] = entry
        self.save()
        return True

    # what is chosen

    def _entry(self, podcast: Podcast) -> dict:
        entry = self.podcasts.get(podcast.name)
        return entry if isinstance(entry, dict) else {}

    def chosen(self, podcast: Podcast) -> bool:
        """Whether the podcast's publishing was set in the GUI, not by default."""
        return isinstance(self._entry(podcast).get("published"), bool)

    def published(self, podcast: Podcast) -> bool:
        entry = self._entry(podcast)
        if isinstance(entry.get("published"), bool):
            return entry["published"]
        if self.exists:
            return False  # new since the choices were made: it waits to be switched on
        return self.default_feeds is None or bool(self.default_feeds & aliases(podcast))

    def auto(self, podcast: Podcast) -> bool:
        entry = self._entry(podcast)
        return entry["auto"] if isinstance(entry.get("auto"), bool) else True

    def category(self, podcast: Podcast) -> str:
        """Its Apple Podcasts category: chosen, from its record, or the default."""
        chosen = self._entry(podcast).get("category")
        for category in (chosen, podcast.category):
            if category in CATEGORIES:
                return category
        return DEFAULT_CATEGORY

    def included(self, podcast: Podcast, episode: Episode) -> bool:
        episodes = self._entry(podcast).get("episodes")
        choice = episodes.get(episode_key(podcast, episode)) if isinstance(episodes, dict) else None
        return choice if isinstance(choice, bool) else self.auto(podcast)

    def choose(self, podcasts: list[Podcast], everything: bool = False) -> list[Podcast]:
        """The podcasts that are published, each with only its chosen episodes.

        ``everything`` publishes each of ``podcasts`` whatever was chosen for
        it — for ``--feed``, which names the podcasts itself.
        """
        return [
            replace(
                p,
                episodes=[e for e in p.episodes if self.included(p, e)],
                category=self.category(p),
            )
            for p in podcasts
            if everything or self.published(p)
        ]

    # changing it

    def set(
        self,
        podcast: Podcast,
        *,
        published: bool | None = None,
        auto: bool | None = None,
        episodes: dict[str, bool] | None = None,
        category: str | None = None,
    ) -> None:
        """Change what is chosen for ``podcast``; what is not given stays.

        ``episodes`` is merged into the choices already made, and choices for
        episodes no longer in the library are let go. Every episode in the
        library now keeps what it has, so ``auto`` only decides for episodes
        that arrive later: switching it off does not take any off the air.
        """
        entry = dict(self._entry(podcast))
        # Each episode as it is now, before anything changes.
        current = {episode_key(podcast, e): self.included(podcast, e) for e in podcast.episodes}
        if published is not None:
            entry["published"] = bool(published)
        elif "published" not in entry:
            # Pin what it was by default, so a later change to the add-on's
            # `feeds` does not undo a choice made on this page.
            entry["published"] = self.published(podcast)
        if auto is not None:
            entry["auto"] = bool(auto)
        if category is not None:
            if category not in CATEGORIES:
                raise ValueError(f"{category!r} is not one of Apple's categories.")
            entry["category"] = category
        current.update({k: bool(v) for k, v in (episodes or {}).items() if k in current})
        entry["episodes"] = current
        self.podcasts[podcast.name] = entry


def episode_key(podcast: Podcast, episode: Episode) -> str:
    """How an episode is known in the choices: its path inside the podcast's folder."""
    return episode.audio.relative_to(podcast.folder).as_posix()
