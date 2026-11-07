"""Tests for the Dropbox helpers that do not need network access."""

import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from dropbox.exceptions import ApiError
from dropbox.files import (
    FileMetadata,
    FolderMetadata,
    ListFolderError,
    ListFolderResult,
    LookupError,
    WriteMode,
)
from dropbox.sharing import (
    CreateSharedLinkWithSettingsError,
    FileLinkMetadata,
    LinkPermissions,
    SharedLinkAlreadyExistsMetadata,
)

from db_podcast.remote import (
    HASH_BLOCK_SIZE,
    DropboxRemote,
    authorization,
    content_hash,
    direct_url,
)


def test_content_hash_empty_file(tmp_path):
    """Test the hash of an empty file, which Dropbox documents."""
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert content_hash(path) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_content_hash_is_a_hash_of_block_hashes(tmp_path):
    """Test that a file is hashed per 4 MB block, and the hashes hashed again."""
    blocks = [b"a" * HASH_BLOCK_SIZE, b"b" * 10]
    path = tmp_path / "two-blocks"
    path.write_bytes(b"".join(blocks))
    expected = hashlib.sha256(
        b"".join(hashlib.sha256(block).digest() for block in blocks)
    ).hexdigest()
    assert content_hash(path) == expected


@pytest.mark.parametrize(
    "shared_link",
    [
        "https://www.dropbox.com/scl/fi/abc123/ep.mp3?rlkey=k3y&dl=0",
        "https://www.dropbox.com/scl/fi/abc123/ep.mp3?rlkey=k3y&dl=1",
        "https://www.dropbox.com/scl/fi/abc123/ep.mp3?rlkey=k3y",
        "https://www.dropbox.com/scl/fi/abc123/ep.mp3?rlkey=k3y&raw=1",
    ],
)
def test_direct_url(shared_link):
    """Test that a shared link becomes a link to the file, keeping its key."""
    assert direct_url(shared_link) == (
        "https://www.dropbox.com/scl/fi/abc123/ep.mp3?rlkey=k3y&raw=1"
    )


@pytest.mark.parametrize("root", ["", "/", "//"])
def test_remote_refuses_the_top_folder(root):
    """Test that the sync cannot be pointed at a whole Dropbox."""
    with pytest.raises(ValueError):
        DropboxRemote("key", "token", root)


class StubClient:
    """Stands in for ``dropbox.Dropbox``, answering with the SDK's own types."""

    def __init__(self):
        self.calls = []
        self.listing = []
        self.shared = []
        self.share_error = None

    def files_list_folder(self, path, recursive):
        self.calls.append(("list", path, recursive))
        if not self.listing:
            raise ApiError("id", ListFolderError.path(LookupError.not_found), None, None)
        return ListFolderResult(self.listing[:1], "cursor", has_more=len(self.listing) > 1)

    def files_list_folder_continue(self, cursor):
        return ListFolderResult(self.listing[1:], "cursor", has_more=False)

    def files_upload(self, data, path, mode, mute):
        self.calls.append(("upload", path, len(data), mode))

    def files_upload_session_start(self, data):
        self.calls.append(("start", len(data)))
        return SimpleNamespace(session_id="session")

    def files_upload_session_append_v2(self, data, cursor):
        self.calls.append(("append", len(data), cursor.offset))

    def files_upload_session_finish(self, data, cursor, commit):
        self.calls.append(("finish", len(data), cursor.offset, commit.path))

    def files_delete_v2(self, path):
        self.calls.append(("delete", path))

    def sharing_list_shared_links(self, path=None, cursor=None, direct_only=None):
        self.calls.append(("links", path))
        return SimpleNamespace(links=self.shared, has_more=False)

    def sharing_create_shared_link_with_settings(self, path, settings):
        self.calls.append(("share", path))
        if self.share_error:
            raise ApiError("id", self.share_error, None, None)
        return SimpleNamespace(url=f"https://www.dropbox.com/scl/fi/new{path}?rlkey=new&dl=0")


@pytest.fixture
def stubbed():
    remote = DropboxRemote("key", "token", "/podcasts")
    remote._dropbox = StubClient()
    return remote, remote._dropbox


def _file(path, hash_):
    now = datetime(2026, 1, 1)
    return FileMetadata(
        name=path.rsplit("/", 1)[-1],
        id="id:1",
        client_modified=now,
        server_modified=now,
        rev="0123456789abcdef",
        size=1,
        path_lower=path.lower(),
        path_display=path,
        content_hash=hash_,
    )


def test_list_of_missing_folder_is_empty(stubbed):
    """Test that a first run, before the folder exists, sees no files."""
    remote, _ = stubbed
    assert remote.list() == {}


def test_list_returns_paths_below_the_root(stubbed):
    """Test that all pages are read and folders are left out."""
    remote, client = stubbed
    client.listing = [
        FolderMetadata(name="show", id="id:0", path_lower="/podcasts/show"),
        _file("/Podcasts/show/a.mp3", "a" * 64),
        _file("/Podcasts/show/feed.xml", "b" * 64),
    ]
    assert remote.list() == {"show/a.mp3": "a" * 64, "show/feed.xml": "b" * 64}
    assert client.calls[0] == ("list", "/podcasts", True)


def test_upload_small_file_in_one_request(stubbed, tmp_path):
    """Test that a file is uploaded over what is there."""
    remote, client = stubbed
    path = tmp_path / "a.mp3"
    path.write_bytes(b"audio")
    remote.upload(path, "show/a.mp3")
    assert client.calls == [("upload", "/podcasts/show/a.mp3", 5, WriteMode.overwrite)]


def test_upload_large_file_in_chunks(stubbed, tmp_path, monkeypatch):
    """Test that a file over the limit is sent as a session, to the last byte."""
    monkeypatch.setattr("db_podcast.remote.SINGLE_UPLOAD_LIMIT", 10)
    monkeypatch.setattr("db_podcast.remote.UPLOAD_CHUNK_SIZE", 4)
    remote, client = stubbed
    path = tmp_path / "a.mp3"
    path.write_bytes(b"x" * 11)
    remote.upload(path, "show/a.mp3")
    assert client.calls == [
        ("start", 4),
        ("append", 4, 4),
        ("finish", 3, 8, "/podcasts/show/a.mp3"),
    ]


def test_url_reuses_an_existing_link(stubbed):
    """Test that a file that is shared already keeps its URL."""
    remote, client = stubbed
    client.shared = [
        SimpleNamespace(
            path_lower="/podcasts/show/feed.xml",
            url="https://www.dropbox.com/scl/fi/old/feed.xml?rlkey=old&dl=0",
        )
    ]
    expected = "https://www.dropbox.com/scl/fi/old/feed.xml?rlkey=old&raw=1"
    assert remote.url("Show/feed.xml") == expected
    assert ("share", "/podcasts/Show/feed.xml") not in client.calls


def test_url_shares_a_new_file_once(stubbed):
    """Test that a link is made for a file without one, and remembered."""
    remote, client = stubbed
    first = remote.url("show/a.mp3")
    assert first == "https://www.dropbox.com/scl/fi/new/podcasts/show/a.mp3?rlkey=new&raw=1"
    assert remote.url("show/a.mp3") == first
    assert client.calls.count(("share", "/podcasts/show/a.mp3")) == 1


@pytest.mark.parametrize("in_error", [True, False])
def test_url_when_dropbox_says_the_link_exists(stubbed, in_error):
    """Test that "already shared" is answered with the existing link."""
    remote, client = stubbed
    now = datetime(2026, 1, 1)
    # Without a path, as for a link the listing of all links did not match.
    existing = FileLinkMetadata(
        url="https://www.dropbox.com/scl/fi/old/a.mp3?rlkey=old&dl=0",
        name="a.mp3",
        link_permissions=LinkPermissions(),
        client_modified=now,
        server_modified=now,
        rev="0123456789abcdef",
        size=1,
    )
    client.share_error = CreateSharedLinkWithSettingsError.shared_link_already_exists(
        SharedLinkAlreadyExistsMetadata.metadata(existing) if in_error else None
    )
    client.shared = [existing]
    assert remote.url("show/a.mp3") == "https://www.dropbox.com/scl/fi/old/a.mp3?rlkey=old&raw=1"
    assert ("share", "/podcasts/show/a.mp3") in client.calls


def test_authorization_asks_for_offline_access():
    """Test that the page to open asks for a token that does not expire."""
    url = authorization("my-app-key").start()
    assert "client_id=my-app-key" in url
    assert "token_access_type=offline" in url
    assert "code_challenge=" in url
