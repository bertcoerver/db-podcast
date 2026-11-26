"""``db-podcast serve``: sync on a schedule, and show the podcasts in a web GUI.

One long-lived process instead of a scheduler loop in a shell script: it syncs
at the times in :data:`SCHEDULE_ENV`, and serves a page — the Home Assistant
add-on's sidebar panel, through ingress — listing every podcast in the
library, whether it is published, and the URL to subscribe to it with, with
buttons to sync, dry-run or check now. Each podcast has a tab of its own, where
it is switched on or off and its episodes are ticked; those choices are kept
in a file of db-podcast's (see :mod:`db_podcast.selection`), and the next sync
publishes them.

The subscribe URLs are what a sync hands back; they are kept in
:data:`URLS_ENV`'s file so the panel still shows them after a restart. Only one
job runs at a time. The GUI answers only the addresses in :data:`GUI_FROM_ENV`:
this machine by default, Home Assistant's ingress proxy in the add-on.

One more endpoint is for fetchit rather than people: ``POST /api/fetched``
with ``{"feeds": ["Bureau Buitenland", ...]}``, its word that it has just
downloaded something for these feeds. If any of them is a podcast that is
published, a sync starts — or, if a job is under way, one more follows it. It
answers the GUI's addresses, and any client that presents
:data:`API_TOKEN_ENV` as a bearer token: fetchit's address on Home Assistant's
network changes, so it cannot be told apart by that.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from db_podcast import __version__, feed, library, sync
from db_podcast.config import ConfigError, get_config
from db_podcast.remote import DropboxRemote, RemoteError
from db_podcast.selection import Selection, episode_key

logger = logging.getLogger("db_podcast")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

SCHEDULE_ENV = "DB_PODCAST_SCHEDULE"
GUI_FROM_ENV = "DB_PODCAST_GUI_FROM"
API_TOKEN_ENV = "DB_PODCAST_API_TOKEN"
URLS_ENV = "DB_PODCAST_URLS"
DEFAULT_FROM = "127.0.0.1,::1"
DEFAULT_URLS = "~/.cache/db-podcast/feed-urls.json"


def parse_schedule(value: str | None) -> list[tuple[int, int]]:
    """``"02:00, 07:00"`` as ``[(2, 0), (7, 0)]``; nonsense is refused."""
    slots = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        hour, sep, minute = part.partition(":")
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            h = m = -1
        if not sep or not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"{part!r} is not a time like 06:30.")
        slots.append((h, m))
    return sorted(set(slots))


def next_slot(slots: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The first slot after ``now``, in ``now``'s time zone."""
    for days in (0, 1):
        day = now.date() + timedelta(days=days)
        for h, m in slots:
            when = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if when > now:
                return when
    return None


def parse_networks(value: str) -> list:
    nets = []
    for part in (value or "").split(","):
        if part.strip():
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                logger.warning("%r is not an address; ignored.", part.strip())
    return nets


def allowed(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks if net.version == ip.version)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


class _JobLog(logging.Handler):
    """Hands what db-podcast logs to the job that is running, as well."""

    def __init__(self) -> None:
        super().__init__()
        self.sink: list[str] | None = None
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        sink = self.sink
        if sink is not None:
            sink.append(self.format(record) + "\n")


@dataclass
class Job:
    id: str
    kind: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    ok: bool | None = None
    result: str = ""
    output: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "started": _iso(self.started),
            "finished": _iso(self.finished),
            "running": self.finished is None,
            "ok": self.ok,
            "result": self.result,
            "output": "".join(self.output)[-200_000:],
        }


class Service:
    """Everything ``db-podcast serve`` does, apart from speaking HTTP."""

    def __init__(
        self,
        *,
        library_override: Path | None = None,
        schedule: list[tuple[int, int]] | None = None,
        gui_from: str = DEFAULT_FROM,
        urls_file: str = DEFAULT_URLS,
        remote_factory=None,
        api_token: str = "",
    ) -> None:
        self.library_override = library_override
        self.schedule = schedule or []
        self.gui_from = parse_networks(gui_from)
        self.urls_file = Path(os.path.expanduser(urls_file))
        self.remote_factory = remote_factory or _dropbox
        self.api_token = api_token
        self.activity: deque[dict] = deque(maxlen=100)
        self.jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._current: Job | None = None
        self._lock = threading.Lock()
        self._choices = threading.Lock()
        self._stop = threading.Event()
        # A sync asked for while a job runs, to follow it; see sync(again=...).
        self._queue_lock = threading.Lock()
        self._queued = False
        self._log = _JobLog()
        self.urls: dict[str, str] = self._load_urls()

    def config(self):
        config = get_config()
        if self.library_override is not None:
            from dataclasses import replace

            config = replace(config, library=self.library_override)
        return config

    def selection(self, config, podcasts: list[library.Podcast] | None = None) -> Selection:
        """The choices, adopting what is published now if none were made yet."""
        with self._choices:
            choices = Selection.load(config.selection, config.feeds)
            if podcasts is not None and choices.adopt(podcasts):
                logger.info("Wrote down what is published now in %s; new podcasts wait "
                            "to be switched on.", config.selection)
            return choices

    # life cycle

    def start(self) -> None:
        # What a sync reports is INFO, and the panel shows it whoever set up
        # logging, or nobody did.
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        logger.addHandler(self._log)
        if self.schedule:
            threading.Thread(target=self._schedule_loop, daemon=True, name="db-podcast-schedule").start()

    def stop(self) -> None:
        self._stop.set()
        logger.removeHandler(self._log)

    # subscribe URLs, kept between restarts

    def _load_urls(self) -> dict[str, str]:
        try:
            data = json.loads(self.urls_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save_urls(self) -> None:
        try:
            self.urls_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.urls_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.urls, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.urls_file)
        except OSError as exc:
            logger.warning("Could not keep the feed URLs in %s: %s", self.urls_file, exc)

    # jobs

    def start_job(self, kind: str, work) -> Job:
        """Run ``work()`` on a thread, unless a job is going — then that job."""
        return self._start_job(kind, work)[0]

    def _start_job(self, kind: str, work) -> tuple[Job, bool]:
        """:meth:`start_job`, saying as well whether the job is a new one."""
        with self._lock:
            if self._current is not None and self._current.finished is None:
                return self._current, False
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._current = job
            self.jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > 20:
                self.jobs.pop(self._order.popleft(), None)

        def body() -> None:
            self._log.sink = job.output
            try:
                job.result = str(work())
                job.ok = True
            except (ConfigError, library.LibraryError, RemoteError) as error:
                job.ok = False
                job.result = str(error)
            except Exception as exc:  # reported in the GUI
                job.ok = False
                job.result = f"{type(exc).__name__}: {exc}"
                job.output.append("\n" + traceback.format_exc())
            finally:
                self._log.sink = None
                job.finished = time.time()
                self.activity.appendleft(
                    {"when": _iso(job.finished), "kind": kind, "text": job.result, "ok": bool(job.ok)}
                )
                if not job.ok:
                    logger.error("%s: %s", kind, job.result)

        threading.Thread(target=body, daemon=True, name=f"db-podcast-{kind}").start()
        return job, True

    def current(self) -> Job | None:
        job = self._current
        return job if job is not None and job.finished is None else None

    def sync(self, *, dry_run: bool = False, again: bool = False) -> Job:
        """Start a sync, or hand back the job under way.

        ``again`` is for news that arrived after the job under way may already
        have read the library: one more sync is queued behind it (only one,
        however many such calls come in meanwhile). It is not for a dry run,
        which publishes nothing.
        """
        def work() -> str:
            config = self.config()
            published = sync.sync(
                config.library,
                self.remote_factory(config),
                dry_run=dry_run,
                selection=self.selection(config, library.scan(config.library)),
                owner_email=config.owner_email,
            )
            for item in published:
                logger.info(
                    "%s: %d episode(s)  %s",
                    item.podcast.title,
                    len(item.podcast.episodes),
                    item.feed_url or "(dry run)",
                )
            if not dry_run:
                self.urls = {item.podcast.name: item.feed_url for item in published if item.feed_url}
                self._save_urls()
            verb = "would be published" if dry_run else "published"
            return f"{len(published)} podcast(s) {verb}."

        if dry_run:
            return self.start_job("dry-run", work)
        with self._queue_lock:
            job, new = self._start_job("sync", work)
            if not new and again and not self._queued:
                self._queued = True
                threading.Thread(target=self._sync_after, args=(job,), daemon=True,
                                 name="db-podcast-queued").start()
            return job

    def queued(self) -> bool:
        """Whether a sync waits for the job under way to finish."""
        return self._queued

    def _sync_after(self, job: Job) -> None:
        while job.finished is None and not self._stop.is_set():
            self._stop.wait(1)
        with self._queue_lock:
            self._queued = False
        if not self._stop.is_set():
            # Should someone have started another job in the meantime, this
            # queues behind that one in turn.
            self.sync(again=True)

    def fetched(self, feeds: list[str] | None) -> dict:
        """fetchit downloaded something for ``feeds``: sync, if any is published.

        The names are fetchit.json's, which are what a podcast's ``feed.json``
        records as its ``name``; a podcast's folder, name in Dropbox and title
        are accepted as well (see :func:`_known_as`), ignoring case and spaces
        around them. No ``feeds`` at all means "something, somewhere", and
        always syncs.
        """
        if feeds:
            config = self.config()
            podcasts = library.scan(config.library)
            choices = self.selection(config, podcasts)
            wanted = {str(f).strip().casefold() for f in feeds}
            ours = sorted({p.name for p in podcasts if choices.published(p) and wanted & _known_as(p)})
            if not ours:
                return {"started": False, "reason": "None of these feeds is a podcast that is published."}
        else:
            ours = []
        what = ", ".join(ours) if ours else "the library"
        job = self.sync(again=True)
        queued = self.queued()
        text = f"fetchit downloaded new episodes for {what}" + ("; a sync follows the job under way." if queued else ".")
        logger.info("%s", text)
        self.activity.appendleft({"when": _iso(time.time()), "kind": "fetched", "text": text, "ok": True})
        return {"started": True, "queued": queued, "podcasts": ours, "job": job.view()}

    def check(self) -> Job:
        def work() -> str:
            config = self.config()
            remote = self.remote_factory(config)
            in_dropbox = {name.lower() for name in remote.list()}
            published = []
            podcasts = library.scan(config.library)
            for podcast in self.selection(config, podcasts).choose(podcasts):
                feed_name = f"{podcast.name}/{library.FEED_FILE}"
                if feed_name.lower() in in_dropbox:
                    published.append(sync.Published(podcast, remote.url(feed_name)))
                else:
                    logger.warning("%s: not in Dropbox yet; sync first.", podcast.name)
            self.urls.update({p.podcast.name: p.feed_url for p in published})
            self._save_urls()
            healthy = sync.check(published)
            return "Every feed and file can be fetched." if healthy else "Something could not be fetched; see above."

        return self.start_job("check", work)

    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            when = next_slot(self.schedule, datetime.now().astimezone())
            if when is None:
                return
            while not self._stop.is_set():
                left = (when - datetime.now().astimezone()).total_seconds()
                if left <= 0:
                    break
                self._stop.wait(min(left, 60))
            if self._stop.is_set():
                return
            job = self.sync()
            while job.finished is None and not self._stop.is_set():
                self._stop.wait(5)
            self._stop.wait(1)

    def next_run(self) -> str | None:
        when = next_slot(self.schedule, datetime.now().astimezone()) if self.schedule else None
        return when.isoformat(timespec="minutes") if when else None

    # what the GUI shows

    def status(self) -> dict:
        config = self.config()
        return {
            "version": __version__,
            "library": str(config.library),
            "library_exists": config.library.is_dir(),
            "feeds": config.feeds,
            "selection": str(config.selection),
            "remote_root": config.remote_root,
            "connected": bool(config.app_key and config.refresh_token),
            "schedule": [f"{h:02d}:{m:02d}" for h, m in self.schedule],
            "next_run": self.next_run(),
            "running": self.current().view() if self.current() else None,
        }

    def podcasts(self) -> list[dict]:
        config = self.config()
        podcasts = library.scan(config.library)
        choices = self.selection(config, podcasts)
        return [self._summary(config, choices, p) for p in podcasts]

    def podcast(self, name: str) -> dict | None:
        """One podcast with all of its episodes, and which of them are published."""
        config = self.config()
        podcasts = library.scan(config.library)
        found = next((p for p in podcasts if p.name == name), None)
        if found is None:
            return None
        return self._detail(config, self.selection(config, podcasts), found)

    def choose(self, name: str, body: dict) -> dict | None:
        """Change what is published of podcast ``name``; see :meth:`Selection.set`."""
        published, auto, category = body.get("published"), body.get("auto"), body.get("category")
        if category is not None and category not in feed.CATEGORIES:
            raise ValueError(f"{category!r} is not one of Apple's categories.")
        episodes = body.get("episodes")
        if published is not None and not isinstance(published, bool):
            raise ValueError("'published' should be true or false.")
        if auto is not None and not isinstance(auto, bool):
            raise ValueError("'auto' should be true or false.")
        if episodes is not None and not (
            isinstance(episodes, dict) and all(isinstance(v, bool) for v in episodes.values())
        ):
            raise ValueError("'episodes' should map episodes to true or false.")
        config = self.config()
        podcasts = library.scan(config.library)
        found = next((p for p in podcasts if p.name == name), None)
        if found is None:
            return None
        self.selection(config, podcasts)
        with self._choices:
            choices = Selection.load(config.selection, config.feeds)
            choices.set(found, published=published, auto=auto, episodes=episodes, category=category)
            choices.save()
        logger.info(
            "%s: %s, %d of %d episode(s) chosen; published at the next sync.",
            found.name,
            "published" if choices.published(found) else "not published",
            sum(choices.included(found, e) for e in found.episodes),
            len(found.episodes),
        )
        return self._detail(config, choices, found)

    def _summary(self, config, choices: Selection, podcast: library.Podcast) -> dict:
        included = [e for e in podcast.episodes if choices.included(podcast, e)]
        newest = podcast.episodes[0] if podcast.episodes else None
        return {
            "name": podcast.name,
            "title": podcast.title,
            "kind": podcast.folder.parent.name if podcast.folder.parent != config.library else "",
            "episodes": len(podcast.episodes),
            "included": len(included),
            "bytes": sum(_size(e.file) for e in included),
            "newest": newest.title if newest else None,
            "newest_date": newest.published.date().isoformat() if newest else None,
            "published": choices.published(podcast),
            "chosen": choices.chosen(podcast),
            "auto": choices.auto(podcast),
            "category": choices.category(podcast),
            "url": self.urls.get(podcast.name),
        }

    def _detail(self, config, choices: Selection, podcast: library.Podcast) -> dict:
        episodes = []
        for episode in podcast.episodes:
            key = episode_key(podcast, episode)
            folder = Path(key).parent.as_posix()
            episodes.append({
                "key": key,
                "title": episode.title,
                "published": episode.published.date().isoformat(),
                "duration": episode.duration,
                "bytes": _size(episode.file),
                "season": "" if folder == "." else folder,
                "number": episode.number,
                "included": choices.included(podcast, episode),
            })
        return {
            "podcast": self._summary(config, choices, podcast),
            "episodes": episodes,
            "categories": list(feed.CATEGORIES),
        }


def _known_as(podcast: library.Podcast) -> set[str]:
    """Every name fetchit might use for ``podcast``, folded for comparing.

    fetchit names a feed by its ``name`` in fetchit.json, and writes that into
    the folder's ``feed.json``; the folder itself is the same name made safe
    for a file system, so the two can differ.
    """
    names = set(library.aliases(podcast))
    try:
        record = json.loads((podcast.folder / library.FETCHIT_FEED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict) and isinstance(record.get("name"), str):
        names.add(record["name"].strip().casefold())
    return names


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dropbox(config) -> DropboxRemote:
    app_key, refresh_token = config.credentials()
    return DropboxRemote(app_key, refresh_token, config.remote_root)


class Handler(BaseHTTPRequestHandler):
    service: Service
    server_version = f"db-podcast/{__version__}"

    def log_message(self, format, *args):  # noqa: A002 - the stdlib's name
        pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, *, token: bool = False) -> bool:
        """Whether this client may be answered; if not, it is told so.

        ``token``: a client elsewhere may also present the API token, as
        ``Authorization: Bearer <token>``.
        """
        if allowed(self.client_address[0], self.service.gui_from):
            return True
        expected = self.service.api_token
        if token and expected:
            scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), expected.encode()):
                return True
            logger.warning("Refused %s from %s: no valid API token.", urlsplit(self.path).path,
                           self.client_address[0])
            self._json(HTTPStatus.FORBIDDEN, {"error": "This needs the API token."})
            return False
        self._json(HTTPStatus.FORBIDDEN, {"error": "The GUI is not open to this address."})
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            # A show's every episode, ticked or not, fits with room to spare.
            data = json.loads(self.rfile.read(min(length, 4_000_000)))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        if not self._allowed():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        svc = self.service
        try:
            if path in ("/", "/index.html"):
                page = resources.files("db_podcast").joinpath("ui/index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/status":
                self._json(HTTPStatus.OK, svc.status())
            elif path == "/api/podcasts":
                self._json(HTTPStatus.OK, {"podcasts": svc.podcasts()})
            elif path == "/api/podcast":
                name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
                detail = svc.podcast(name)
                if detail is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": f"No podcast called {name!r}."})
                else:
                    self._json(HTTPStatus.OK, detail)
            elif path == "/api/activity":
                self._json(HTTPStatus.OK, {"activity": list(svc.activity)})
            elif path.startswith("/api/jobs/"):
                job = svc.jobs.get(path.rsplit("/", 1)[-1])
                if job is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "No such job."})
                else:
                    self._json(HTTPStatus.OK, job.view())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except (ConfigError, library.LibraryError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        path = urlsplit(self.path).path.rstrip("/")
        if not self._allowed(token=path == "/api/fetched"):
            return
        body = self._body()
        if path == "/api/fetched":
            feeds = body.get("feeds")
            if isinstance(feeds, str):
                feeds = [feeds]
            if not isinstance(feeds, list):
                feeds = None
            try:
                self._json(HTTPStatus.ACCEPTED, self.service.fetched(feeds or None))
            except (ConfigError, library.LibraryError) as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        elif path == "/api/sync":
            self._json(HTTPStatus.ACCEPTED, self.service.sync(dry_run=bool(body.get("dry_run"))).view())
        elif path == "/api/check":
            self._json(HTTPStatus.ACCEPTED, self.service.check().view())
        elif path == "/api/podcast":
            name = body.get("name")
            try:
                detail = self.service.choose(name, body) if isinstance(name, str) else None
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except (ConfigError, library.LibraryError, OSError) as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            if detail is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"No podcast called {name!r}."})
            else:
                self._json(HTTPStatus.OK, detail)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})


def make_server(service: Service, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_class = type("Server", (ThreadingHTTPServer,), {"address_family": family, "daemon_threads": True})
    return server_class((host, port), handler)


def serve(
    *,
    library_override: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    on_start: str = "wait",
) -> None:
    """Run the service until interrupted. See the module docstring."""
    service = Service(
        library_override=library_override,
        schedule=parse_schedule(os.environ.get(SCHEDULE_ENV, "")),
        gui_from=os.environ.get(GUI_FROM_ENV, "").strip() or DEFAULT_FROM,
        urls_file=os.environ.get(URLS_ENV, "").strip() or DEFAULT_URLS,
        api_token=os.environ.get(API_TOKEN_ENV, "").strip(),
    )
    try:
        server = make_server(service, host, port)
    except OSError as exc:
        raise SystemExit(f"error: cannot listen on {host}:{port}: {exc.strerror or exc}.")
    service.start()
    when = service.next_run()
    logger.info(
        "db-podcast %s serving on http://%s:%s/ — %s",
        __version__,
        host,
        server.server_address[1],
        f"next sync {when}." if when else "no schedule; syncs only when asked.",
    )
    if on_start in ("run", "dry-run"):
        service.sync(dry_run=on_start == "dry-run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
        print("stopped", file=sys.stderr)
