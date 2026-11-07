"""Command-line interface.

Subcommands:
    auth            connect to Dropbox, once (opens a page, asks for a code)
    sync            mirror the podcasts to Dropbox and publish their feeds
    check           fetch the published feeds and their files like an app would
    serve           sync on a schedule, with a web GUI

Downloading is fetchit's job: point the library at the folder it fills, and
choose which of its feeds become podcasts with ``--feed`` or DB_PODCAST_FEEDS.
"""

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from db_podcast import library, sync
from db_podcast.config import (
    APP_KEY_ENV,
    ENV_FILE,
    REFRESH_TOKEN_ENV,
    Config,
    ConfigError,
    get_config,
    save_dotenv,
)
from db_podcast.remote import DropboxRemote, RemoteError, authorization

logger = logging.getLogger("db_podcast")

APP_CONSOLE_URL = "https://www.dropbox.com/developers/apps"


def _cmd_auth(args: argparse.Namespace, config: Config) -> int:
    app_key = args.app_key or config.app_key
    if not app_key:
        print(f"Create an app at {APP_CONSOLE_URL} (see the README), then paste its")
        app_key = input("App key: ").strip()
    flow = authorization(app_key)
    print("\nOpen this page, allow access, and copy the code it shows:\n")
    print(f"  {flow.start()}\n")
    result = flow.finish(input("Code: ").strip())
    save_dotenv({APP_KEY_ENV: app_key, REFRESH_TOKEN_ENV: result.refresh_token})
    print(f"\nConnected. The app key and token are in {Path(ENV_FILE).resolve()}.")
    return 0


def _cmd_sync(args: argparse.Namespace, config: Config) -> int:
    published = sync.sync(
        config.library,
        _remote(config),
        prune=not args.no_prune,
        dry_run=args.dry_run,
        names=config.feeds,
    )
    if not published:
        logger.warning("No podcasts found in %s.", config.library)
    for item in published:
        episodes = f"{len(item.podcast.episodes)} episode(s)"
        logger.info("%s: %s  %s", item.podcast.title, episodes, item.feed_url or "(dry run)")
    return 0


def _cmd_check(args: argparse.Namespace, config: Config) -> int:
    remote = _remote(config)
    in_dropbox = {name.lower() for name in remote.list()}
    published = []
    for podcast in library.scan(config.library, config.feeds):
        feed_name = f"{podcast.name}/{library.FEED_FILE}"
        if feed_name.lower() in in_dropbox:
            published.append(sync.Published(podcast, remote.url(feed_name)))
        else:
            logger.warning("%s: not in Dropbox yet; run `db-podcast sync`.", podcast.name)
    return 0 if sync.check(published) else 1


def _cmd_serve(args: argparse.Namespace, config: Config) -> int:
    from db_podcast.server import serve

    serve(
        library_override=Path(args.library).expanduser() if args.library else None,
        host=args.host,
        port=args.port,
        on_start=args.on_start,
    )
    return 0


def _remote(config: Config) -> DropboxRemote:
    app_key, refresh_token = config.credentials()
    return DropboxRemote(app_key, refresh_token, config.remote_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="db-podcast", description="Host podcasts in Dropbox.")
    parser.add_argument(
        "--library",
        help="fetchit's library, or a folder with one sub-folder per podcast (default: ~/fetchit)",
    )
    parser.add_argument(
        "--feed",
        action="append",
        metavar="NAME",
        help="publish only this podcast (repeatable; default: $DB_PODCAST_FEEDS, or all)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="connect to Dropbox, once")
    auth.add_argument("--app-key", help="the app key of your Dropbox app")
    auth.set_defaults(run=_cmd_auth)

    sync_ = commands.add_parser("sync", help="mirror the library to Dropbox, publish the feeds")
    sync_.add_argument(
        "--dry-run", action="store_true", help="only say what would be uploaded and deleted"
    )
    sync_.add_argument(
        "--no-prune", action="store_true", help="keep Dropbox files that left the library"
    )
    sync_.set_defaults(run=_cmd_sync)

    check = commands.add_parser("check", help="fetch the published feeds like an app would")
    check.set_defaults(run=_cmd_check)

    serve = commands.add_parser(
        "serve",
        help="sync on a schedule, with a web GUI",
        description="Sync at the times in DB_PODCAST_SCHEDULE (e.g. 02:00,07:00) and serve a GUI.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8099, help="port to listen on (default: 8099)")
    serve.add_argument("--on-start", choices=("wait", "dry-run", "run"), default="wait",
                       help="what to do at once, before the schedule takes over (default: wait)")
    serve.set_defaults(run=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = get_config()
        if args.library:
            config = replace(config, library=Path(args.library).expanduser())
        if args.feed:
            config = replace(config, feeds=args.feed)
        return args.run(args, config)
    except (ConfigError, library.LibraryError, RemoteError) as error:
        logger.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
