# db-podcast — Home Assistant add-on

Publishes chosen feeds from fetchit's library (`/media/fetchit`) as podcasts
through Dropbox, on a schedule. Install the **fetchit** add-on
(`https://github.com/bertcoerver/fetchit`) first: it does the downloading, and
this add-on only reads what it filed.

## Installing

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/bertcoerver/db-podcast`.
2. Install **db-podcast**.

## Connecting to Dropbox

Connecting asks you to open a page and paste a code back, and there is no shell
in an add-on to do that in. So do it once on your own computer:

1. Create the Dropbox app as the README describes (scoped access, App folder,
   the four permissions).
2. `pip install db-podcast`, then run `db-podcast auth`. It writes
   `DB_PODCAST_APP_KEY` and `DB_PODCAST_REFRESH_TOKEN` to `.env`.
3. Copy those two values into the add-on's **Configuration** tab as `app_key`
   and `refresh_token`.

The token is long-lived access to that app's folder in your Dropbox; treat it
like a password.

## Configuration

| Option | Default | |
|---|---|---|
| `app_key` | — | Your Dropbox app's key. |
| `refresh_token` | — | From `db-podcast auth`. |
| `feeds` | (all) | The feeds to publish, by their name in fetchit.json. Empty publishes every audio and video feed in the library. |
| `remote_root` | `/podcasts` | The folder in Dropbox to mirror to. Anything in it that is not published is deleted. |
| `library` | `/media/fetchit` | fetchit's library. |
| `on_start` | `wait` | `wait`, `dry-run` (say what would be uploaded and deleted) or `run`. |
| `schedule` | `02:00`, `07:00`, `15:30` | Local times to sync at — a little after fetchit's runs. |
| `log_level` | `info` | |

Start it, open the panel, and press **Sync now**: each published podcast then
shows the URL to subscribe to it with in a podcast app. The URLs stay the same for as long as
the feeds are published.

## The panel

The add-on is **db-podcast** in Home Assistant's sidebar — switch on **Show in
sidebar** on its Info page — or **Open Web UI**. It lists every podcast in the
library, whether it is published, and the URL to subscribe to it with, with a
button to copy it; and has buttons to sync now, dry-run, or check the published
feeds the way a podcast app would, with what each printed. The URLs are kept in
the add-on's `/data`, so they are there after a restart; a feed's URL stays the
same for as long as it is published.

## Things to know

- **The library is the truth.** An episode fetchit prunes (its `keep_days`) is
  deleted from Dropbox on the next sync, and so is a feed you take out of `feeds`.
  A free Dropbox holds 2 GB: give published feeds a `keep_days` in fetchit.json that
  keeps them inside it.
- **Video feeds work too**, and podcast apps play them, but they fill a Dropbox
  quickly.
- `/media` is mapped read-only: this add-on cannot change the library.
