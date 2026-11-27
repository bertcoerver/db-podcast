# Changelog

## 0.4.2

- Link to the feed and the artwork on Dropbox's content host too

## 0.4.1

- Link to an episode as a download from Dropbox's content host
- Go back to publishing the one file an episode has

## 0.4.0

- Publish an episode's smaller copy where fetchit keeps one

## 0.3.2

- Sync as soon as fetchit says it downloaded something

## 0.3.1

- Scale up podcast covers smaller than 1400 pixels

## 0.3.0

- Publish podcasts from the overview list, tabs only for published ones

## 0.2.1

- Feeds that pass podcast validators

## 0.2.0

- Choose podcasts and episodes in the panel, a tab per podcast

## 0.1.7

- First release installed from the add-on repository.

## 0.1.1

- First release installed from the add-on repository.

## Unreleased

- A podcast cover smaller than 1400 by 1400 pixels, the least Apple accepts,
  is scaled up to that on its way to Dropbox. The library keeps the original.

- Feeds pass podcast validators: an iTunes owner and category, a plain RSS
  image, `itunes:type` and `episodeType`, and `podcast:locked` / `podcast:guid`.
  Choose the category in each podcast's tab; set `owner_email` to name an
  owner address.
- Artwork goes to Dropbox under the suffix of what it is: fetchit's cover.jpg
  that is really a PNG is uploaded as cover.png, and served as one.

- First version as an add-on: syncs chosen feeds from fetchit's library in
  /media/fetchit to Dropbox on a schedule.
- A panel in Home Assistant's sidebar: every podcast, whether it is published,
  its subscribe URL to copy, and sync / dry run / check buttons.
- A tab per podcast in the panel: switch it on or off, tick the episodes to
  publish, and choose whether new episodes are added automatically. The choices
  are kept in /data/selection.json.
- New podcasts in fetchit's library wait, switched off, until you switch them
  on in the panel. What was published before (the `feeds` option) is written
  down the first time, and stays published; `feeds` is not read after that.
