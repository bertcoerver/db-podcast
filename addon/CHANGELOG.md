# Changelog

## 0.2.0

- Choose podcasts and episodes in the panel, a tab per podcast

## 0.1.7

- First release installed from the add-on repository.

## 0.1.1

- First release installed from the add-on repository.

## Unreleased

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
