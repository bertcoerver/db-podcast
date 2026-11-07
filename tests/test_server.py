"""Tests for db_podcast.server (Dropbox is faked; HTTP is real, on localhost)."""

import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from db_podcast import server
from db_podcast.server import Service

from .test_sync import FakeRemote


@pytest.fixture
def running(tmp_path, monkeypatch):
    root = tmp_path / "fetchit"
    (root / ".fetchit").mkdir(parents=True)
    for name in ("Bureau Buitenland", "Other"):
        folder = root / "podcasts" / name
        folder.mkdir(parents=True)
        (folder / "feed.json").write_text(json.dumps({"name": name, "title": name}))
        (folder / "2026-11-06 - Ep.mp3").write_bytes(b"ep")
        (folder / "2026-11-06 - Ep.json").write_text(json.dumps({"title": "Ep", "published": "2026-11-06T08:00:00+00:00"}))
    monkeypatch.setenv("DB_PODCAST_LIBRARY", str(root))
    monkeypatch.setenv("DB_PODCAST_FEEDS", "Bureau Buitenland")
    monkeypatch.setenv("DB_PODCAST_APP_KEY", "key")
    monkeypatch.setenv("DB_PODCAST_REFRESH_TOKEN", "token")
    monkeypatch.chdir(tmp_path)  # no stray .env
    remote = FakeRemote()
    started = []

    def start(**kwargs):
        service = Service(urls_file=str(tmp_path / "urls.json"), remote_factory=lambda config: remote, **kwargs)
        httpd = server.make_server(service, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        service.start()
        started.append((httpd, service))
        return f"http://127.0.0.1:{httpd.server_address[1]}", service

    yield start, remote, tmp_path
    for httpd, service in started:
        httpd.shutdown()
        httpd.server_close()
        service.stop()


def _call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _wait(base, job):
    for _ in range(200):
        _, job = _call(base, f"/api/jobs/{job['id']}")
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_status_and_page(running):
    start, _, _ = running
    base, _ = start(schedule=[(2, 0)])
    with urlopen(base + "/", timeout=10) as response:
        assert "<title>db-podcast</title>" in response.read().decode()
    status, info = _call(base, "/api/status")
    assert status == 200 and info["connected"] and info["feeds"] == ["Bureau Buitenland"]
    assert info["schedule"] == ["02:00"]


def test_a_sync_from_the_gui_publishes_and_shows_the_subscribe_url(running):
    start, remote, tmp_path = running
    base, _ = start()

    _, before = _call(base, "/api/podcasts")
    assert [(p["name"], p["published"], p["url"]) for p in before["podcasts"]] == [
        ("Bureau Buitenland", True, None),
        ("Other", False, None),
    ]

    status, job = _call(base, "/api/sync", {})
    assert status == 202
    job = _wait(base, job)
    assert job["ok"] and job["result"] == "1 podcast(s) published."
    assert "Bureau Buitenland: 1 episode(s)" in job["output"]

    _, after = _call(base, "/api/podcasts")
    url = after["podcasts"][0]["url"]
    assert url == remote.links["bureau buitenland/feed.xml"]
    # Kept for after a restart.
    assert json.loads((tmp_path / "urls.json").read_text()) == {"Bureau Buitenland": url}
    base2, _ = start()
    _, again = _call(base2, "/api/podcasts")
    assert again["podcasts"][0]["url"] == url


def test_a_failing_sync_says_why(running, monkeypatch):
    start, _, _ = running
    monkeypatch.delenv("DB_PODCAST_REFRESH_TOKEN")
    base, service = start()
    service.remote_factory = server._dropbox
    job = _wait(base, _call(base, "/api/sync", {})[1])
    assert job["ok"] is False and "db-podcast auth" in job["result"]


def test_the_gui_is_only_for_its_own_addresses(running):
    start, _, _ = running
    base, _ = start(gui_from="10.9.9.9")
    assert _call(base, "/api/status")[0] == 403
