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
| `feeds` | (all) | Read once, the first time the add-on finds the library: which podcasts start out published. Names from fetchit.json; empty is every audio and video feed. After that, the panel decides. |
| `remote_root` | `/podcasts` | The folder in Dropbox to mirror to. Anything in it that is not published is deleted. |
| `library` | `/media/fetchit` | fetchit's library. |
| `on_start` | `wait` | `wait`, `dry-run` (say what would be uploaded and deleted) or `run`. |
| `schedule` | `02:00`, `07:00`, `15:30` | Local times to sync at — a little after fetchit's runs. |
| `log_level` | `info` | |

Start it, open the panel, choose what to publish in each podcast's tab, and
press **Sync now**: each published podcast then shows the URL to subscribe to it
with in a podcast app. The URLs stay the same for as long as
the feeds are published.

## The panel

The add-on is **db-podcast** in Home Assistant's sidebar — switch on **Show in
sidebar** on its Info page — or **Open Web UI**. It lists every podcast in the
library, whether it is published, and the URL to subscribe to it with, with a
button to copy it; and has buttons to sync now, dry-run, or check the published
feeds the way a podcast app would, with what each printed. The URLs are kept in
the add-on's `/data`, so they are there after a restart; a feed's URL stays the
same for as long as it is published.

Every podcast fetchit has filed has a tab of its own, with every episode in the
library:

- **Publish this podcast** switches the whole podcast on or off.
- **Add new episodes automatically**: on, every episode fetchit downloads is
  published at the next sync; off, new episodes wait in the tab, unticked.
  Switching it only decides for episodes still to come.
- **A tick per episode** says whether it is in the feed. Shift-click ticks a
  range.

Choices are saved as you click, in `/data/selection.json`, and Dropbox follows
at the next sync (or **Sync now**). A podcast that appears in fetchit's library
later starts **switched off**: it is listed with its episodes, but nothing goes to
Dropbox until you switch it on.

The first time the add-on finds podcasts in the library it writes down what is
published then — what the `feeds` option said — so updating to this version takes
nothing off the air.

## Things to know

- **The library is the truth.** An episode fetchit prunes (its `keep_days`) is
  deleted from Dropbox on the next sync, and so is an episode you untick or a
  podcast you switch off. Switching a podcast off and on again gives it a new
  subscribe URL.
  A free Dropbox holds 2 GB: give published feeds a `keep_days` in fetchit.json that
  keeps them inside it.
- **Video feeds work too**, and podcast apps play them, but they fill a Dropbox
  quickly.
- `/media` is mapped read-only: this add-on cannot change the library.
