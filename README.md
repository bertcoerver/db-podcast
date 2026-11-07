# db-podcast

Host podcasts in Dropbox.

db-podcast publishes chosen feeds from a media library as podcasts.
`db-podcast sync` mirrors their files to Dropbox, gets the public URL of every
file, and writes and uploads a feed per podcast that links to them. The feed's
own URL is what you subscribe to in a podcast app.

The library is the one [fetchit](https://github.com/bertcoerver/fetchit)
downloads into: it reads RSS and podcast feeds, ARTE, NPO Start, YouTube and
Bayam magazine links, and files them in a folder that db-podcast, rssignal and
Jellyfin all read. db-podcast downloads nothing itself, and never writes to the
library.

## Installation

```bash
pip install db-podcast
```

## Connecting to Dropbox (once)

A free Dropbox account is enough. It holds 2 GB, and Dropbox pauses shared
links that are downloaded more than 20 GB a day.

1. Go to <https://www.dropbox.com/developers/apps> and choose **Create app**:
   **Scoped access**, **App folder**, and any name. With "App folder" the app
   can only see `Apps/<name>` in your Dropbox, and nothing else in it.
2. On the app's **Permissions** tab tick `files.metadata.read`,
   `files.content.write`, `sharing.read` and `sharing.write`, and press
   **Submit**. Do this before the next step: a token only carries the
   permissions the app had when it was made.
3. Copy the **App key** from the **Settings** tab and run:

   ```bash
   db-podcast auth
   ```

   It asks for the key, shows a page to open, and asks for the code that page
   gives you. The key and a long-lived token are written to `.env` in the
   current folder. The app secret is not needed.

## The library

Point db-podcast at fetchit's library — `~/fetchit` by default, `/media/fetchit`
on Home Assistant — and choose which of its feeds to publish:

```bash
export DB_PODCAST_LIBRARY=~/fetchit
export DB_PODCAST_FEEDS="Bureau Buitenland, Mes premières Belles Histoires"
```

A podcast is any feed in `podcasts/` (audio) or `shows/` (video — podcast apps play
video too) that has something to play. Leave `DB_PODCAST_FEEDS` empty to publish
all of them. Names are the feeds' names in fetchit.json, or their titles, ignoring
case. Titles, descriptions, dates, episode numbers and artwork all come from what
fetchit recorded beside each file; a series keeps its season folders in Dropbox.

A feed that has both audio and video in the library is two podcasts; the video one
is called `<name> (shows)`.

A folder made by hand works too, one sub-folder per podcast:

```
~/Podcasts/
    my-show/
        podcast.json            optional: title, description, author, language, link, explicit
        cover.png               optional: the podcast's artwork
        un-petit-air.mp3        an episode
        un-petit-air.png        optional: its artwork (same name as the audio)
        un-petit-air.json       optional: title, description, published (ISO date), number
```

A folder with audio files in it is already a podcast. Without the JSON files,
titles come from the audio tags or the file names, dates from the files'
modification times, and the newest episode's artwork doubles as the podcast's.

Keep the library out of iCloud Drive and similar: a file the cloud has evicted
from disk looks deleted, and the sync would delete it from Dropbox too.

## Usage

```bash
db-podcast sync             # upload what is new or changed, delete what is gone, publish the feeds
db-podcast sync --dry-run   # only say what would be uploaded and deleted
db-podcast check            # fetch the published feeds and their files like an app would
```

`sync` ends with the URL of each feed. It stays the same for as long as the
feed is not deleted from Dropbox, so subscribing is a one-time thing. Anyone
who has the URL can listen; nobody can find it without.

The library is the truth: a file removed from it — pruned by fetchit's
`keep_days`, say — is removed from Dropbox on the next `sync` (`--no-prune` keeps
it), and so is a feed you stop publishing. An episode's URL lives and dies with
its file. If the library is missing or empty, nothing is deleted.

```bash
db-podcast --feed "Bureau Buitenland" sync   # just this one, this time
```

The feed itself is built in a temporary folder and uploaded from there; nothing is
written to the library. What Dropbox already has is compared by content, so an
unchanged feed is not uploaded again.

### Bayam

Bayam magazine links are a fetchit source now: put them in a feed's `urls` in
fetchit.json, and publish that feed here. See fetchit's README.

### Configuration

Environment variables, or lines in `.env` (see `.env.example`):

| Variable | Default | |
|---|---|---|
| `DB_PODCAST_APP_KEY` | | written by `db-podcast auth` |
| `DB_PODCAST_REFRESH_TOKEN` | | written by `db-podcast auth` |
| `DB_PODCAST_LIBRARY` | `~/fetchit` | also `--library` |
| `DB_PODCAST_FEEDS` | all | comma-separated feed names; also `--feed`, repeatable |
| `DB_PODCAST_REMOTE_ROOT` | `/podcasts` | the folder in Dropbox to mirror to |

## Development

```bash
pip install -e ".[dev]"
pytest
```

### As a service, with a GUI

```bash
DB_PODCAST_SCHEDULE=02:00,07:00,15:30 db-podcast serve   # http://127.0.0.1:8099/
```

`db-podcast serve` stays running, syncs at those times, and serves a page listing
every podcast, whether it is published, and its subscribe URL, with sync / dry
run / check buttons. Only this machine may open it, unless `DB_PODCAST_GUI_FROM`
lists other addresses.

## Running it on Home Assistant

[`addon/`](addon/DOCS.md) is a Home Assistant add-on that syncs on a schedule,
next to the fetchit add-on that fills the library.

## Release

```bash
./tag_release.sh patch             # or minor / major: the Python package
make addon-release VERSION=x.y.z   # the Home Assistant add-on
```
