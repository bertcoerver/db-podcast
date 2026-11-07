"""The Dropbox folder the library is mirrored to, and the public URLs of its files.

Dropbox serves no folder over HTTP. What it does have is a shared link per
file, with a key in it that cannot be derived from the file's name::

    https://www.dropbox.com/scl/fi/<id>/episode.mp3?rlkey=<key>&dl=0

Opened as is, that is a preview page. With ``raw=1`` in place of ``dl=0`` it
is the file itself, which is what goes into the feed. The link belongs to the
file, not to its content: uploading a new version over it keeps the URL, so a
feed's URL stays the same for as long as the feed is not deleted.
"""

import hashlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import dropbox
from dropbox.exceptions import ApiError, DropboxException
from dropbox.files import CommitInfo, FileMetadata, UploadSessionCursor, WriteMode
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from dropbox.sharing import LinkAudience, RequestedLinkAccessLevel, SharedLinkSettings

# Dropbox's content hash is a hash of the hashes of blocks this size.
HASH_BLOCK_SIZE = 4 * 1024 * 1024
# Above this a file has to be sent in pieces, as an upload session.
SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024

RemoteError = DropboxException


class DropboxRemote:
    """The files below one folder in Dropbox, addressed relative to it.

    Parameters
    ----------
    app_key
        The key of the Dropbox app (see ``db-podcast auth``).
    refresh_token
        The token ``db-podcast auth`` obtained for that app.
    root
        The folder to mirror the library into, e.g. ``/podcasts``. For an app
        with "App folder" access this is inside ``Apps/<app name>``.
    """

    def __init__(self, app_key: str, refresh_token: str, root: str):
        root = root.strip("/")
        if not root:
            # Syncing deletes what is not in the library, which must never
            # mean everything in somebody's Dropbox.
            raise ValueError("The Dropbox folder to sync to cannot be the top folder")
        self._root = "/" + root
        self._dropbox = dropbox.Dropbox(oauth2_refresh_token=refresh_token, app_key=app_key)
        self._links: dict[str, str] | None = None

    def list(self) -> dict[str, str]:
        """Return the content hash of every file, by its path below the root."""
        try:
            result = self._dropbox.files_list_folder(self._root, recursive=True)
        except ApiError as error:
            if error.error.is_path() and error.error.get_path().is_not_found():
                return {}
            raise
        found = {}
        while True:
            for entry in result.entries:
                if isinstance(entry, FileMetadata):
                    found[entry.path_display[len(self._root) + 1 :]] = entry.content_hash
            if not result.has_more:
                return found
            result = self._dropbox.files_list_folder_continue(result.cursor)

    def upload(self, path: Path, name: str) -> None:
        """Upload the file ``path`` as ``name``, replacing what is there."""
        target = self._path(name)
        size = path.stat().st_size
        with path.open("rb") as file:
            if size <= SINGLE_UPLOAD_LIMIT:
                self._dropbox.files_upload(
                    file.read(), target, mode=WriteMode.overwrite, mute=True
                )
                return
            session = self._dropbox.files_upload_session_start(file.read(UPLOAD_CHUNK_SIZE))
            cursor = UploadSessionCursor(session.session_id, offset=file.tell())
            while size - file.tell() > UPLOAD_CHUNK_SIZE:
                self._dropbox.files_upload_session_append_v2(
                    file.read(UPLOAD_CHUNK_SIZE), cursor
                )
                cursor.offset = file.tell()
            commit = CommitInfo(target, mode=WriteMode.overwrite, mute=True)
            self._dropbox.files_upload_session_finish(
                file.read(UPLOAD_CHUNK_SIZE), cursor, commit
            )

    def delete(self, name: str) -> None:
        """Delete the file or folder ``name``."""
        self._dropbox.files_delete_v2(self._path(name))

    def url(self, name: str) -> str:
        """Return the public URL of the file ``name``, sharing it if need be."""
        path = self._path(name)
        if self._links is None:
            self._links = self._shared_links()
        key = path.lower()
        if key not in self._links:
            self._links[key] = self._share(path)
        return direct_url(self._links[key])

    def _path(self, name: str) -> str:
        return f"{self._root}/{name}"

    def _shared_links(self) -> dict[str, str]:
        """Return the shared link of every file that has one, by lower-case path."""
        links = {}
        result = self._dropbox.sharing_list_shared_links()
        while True:
            for link in result.links:
                if link.path_lower:
                    links.setdefault(link.path_lower, link.url)
            if not result.has_more:
                return links
            result = self._dropbox.sharing_list_shared_links(cursor=result.cursor)

    def _share(self, path: str) -> str:
        settings = SharedLinkSettings(
            audience=LinkAudience.public, access=RequestedLinkAccessLevel.viewer
        )
        try:
            return self._dropbox.sharing_create_shared_link_with_settings(path, settings).url
        except ApiError as error:
            if not error.error.is_shared_link_already_exists():
                raise
            # Shared after all; the error usually says with which link.
            existing = error.error.get_shared_link_already_exists()
            if existing is not None and existing.is_metadata():
                return existing.get_metadata().url
            return self._dropbox.sharing_list_shared_links(path, direct_only=True).links[0].url


def authorization(app_key: str) -> DropboxOAuth2FlowNoRedirect:
    """Start authorizing the app ``app_key`` for long-lived access to a Dropbox.

    ``start()`` gives the page to open; ``finish(code)``, with the code that
    page shows, gives a result with the ``refresh_token``. This is the PKCE
    flow, so the app's secret is not needed and is not stored anywhere.
    """
    return DropboxOAuth2FlowNoRedirect(app_key, use_pkce=True, token_access_type="offline")


def direct_url(shared_link: str) -> str:
    """Turn a shared link into the URL of the file itself."""
    parts = urlsplit(shared_link)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key not in ("dl", "raw")]
    query.append(("raw", "1"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def content_hash(path: Path) -> str:
    """Return the hash Dropbox reports for a file with the content of ``path``."""
    block_hashes = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(HASH_BLOCK_SIZE):
            block_hashes.update(hashlib.sha256(block).digest())
    return block_hashes.hexdigest()
