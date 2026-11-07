"""``db-podcast serve``: sync on a schedule, and show the podcasts in a web GUI.

One long-lived process instead of a scheduler loop in a shell script: it syncs
at the times in :data:`SCHEDULE_ENV`, and serves a page — the Home Assistant
add-on's sidebar panel, through ingress — listing every podcast in the
library, whether it is published, and the URL to subscribe to it with, with
buttons to sync, dry-run or check now.

The subscribe URLs are what a sync hands back; they are kept in
:data:`URLS_ENV`'s file so the panel still shows them after a restart. Only one
job runs at a time. The GUI answers only the addresses in :data:`GUI_FROM_ENV`:
this machine by default, Home Assistant's ingress proxy in the add-on.
"""

from __future__ import annotations

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
from urllib.parse import urlsplit

from db_podcast import __version__, library, sync
from db_podcast.config import ConfigError, get_config
from db_podcast.remote import DropboxRemote, RemoteError

logger = logging.getLogger("db_podcast")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

SCHEDULE_ENV = "DB_PODCAST_SCHEDULE"
GUI_FROM_ENV = "DB_PODCAST_GUI_FROM"
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
    ) -> None:
        self.library_override = library_override
        self.schedule = schedule or []
        self.gui_from = parse_networks(gui_from)
        self.urls_file = Path(os.path.expanduser(urls_file))
        self.remote_factory = remote_factory or _dropbox
        self.activity: deque[dict] = deque(maxlen=100)
        self.jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._current: Job | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._log = _JobLog()
        self.urls: dict[str, str] = self._load_urls()

    def config(self):
        config = get_config()
        if self.library_override is not None:
            from dataclasses import replace

            config = replace(config, library=self.library_override)
        return config

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
        with self._lock:
            if self._current is not None and self._current.finished is None:
                return self._current
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
        return job

    def current(self) -> Job | None:
        job = self._current
        return job if job is not None and job.finished is None else None

    def sync(self, *, dry_run: bool = False) -> Job:
        def work() -> str:
            config = self.config()
            published = sync.sync(config.library, self.remote_factory(config), dry_run=dry_run, names=config.feeds)
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

        return self.start_job("dry-run" if dry_run else "sync", work)

    def check(self) -> Job:
        def work() -> str:
            config = self.config()
            remote = self.remote_factory(config)
            in_dropbox = {name.lower() for name in remote.list()}
            published = []
            for podcast in library.scan(config.library, config.feeds):
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
            "remote_root": config.remote_root,
            "connected": bool(config.app_key and config.refresh_token),
            "schedule": [f"{h:02d}:{m:02d}" for h, m in self.schedule],
            "next_run": self.next_run(),
            "running": self.current().view() if self.current() else None,
        }

    def podcasts(self) -> list[dict]:
        config = self.config()
        podcasts = library.scan(config.library)
        wanted = {n.strip().casefold() for n in config.feeds} if config.feeds else None
        out = []
        for podcast in podcasts:
            names = {podcast.folder.name.casefold(), podcast.name.casefold(), podcast.title.casefold()}
            size = 0
            for path in podcast.files:
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
            newest = podcast.episodes[0] if podcast.episodes else None
            out.append({
                "name": podcast.name,
                "title": podcast.title,
                "kind": podcast.folder.parent.name if podcast.folder.parent != config.library else "",
                "episodes": len(podcast.episodes),
                "bytes": size,
                "newest": newest.title if newest else None,
                "newest_date": newest.published.date().isoformat() if newest else None,
                "published": wanted is None or bool(wanted & names),
                "url": self.urls.get(podcast.name),
            })
        return out


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

    def _allowed(self) -> bool:
        if allowed(self.client_address[0], self.service.gui_from):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "The GUI is not open to this address."})
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(min(length, 100_000)))
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
        if not self._allowed():
            return
        path = urlsplit(self.path).path.rstrip("/")
        body = self._body()
        if path == "/api/sync":
            self._json(HTTPStatus.ACCEPTED, self.service.sync(dry_run=bool(body.get("dry_run"))).view())
        elif path == "/api/check":
            self._json(HTTPStatus.ACCEPTED, self.service.check().view())
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
