# shellcheck shell=bash
#
# The environment every db-podcast command in this add-on runs with. Expects
# bashio to be loaded.

export HOME="/data/home"
mkdir -p "${HOME}"
# Run from /data so a stray .env lookup finds nothing of anybody else's.
cd /data

DB_PODCAST_APP_KEY="$(bashio::config 'app_key')"
DB_PODCAST_REFRESH_TOKEN="$(bashio::config 'refresh_token')"
DB_PODCAST_LIBRARY="$(bashio::config 'library')"
DB_PODCAST_REMOTE_ROOT="$(bashio::config 'remote_root')"
# The list as db-podcast reads it: comma-separated, names and spaces and all.
DB_PODCAST_FEEDS="$(jq -r '(.feeds // []) | join(",")' /data/options.json)"
export DB_PODCAST_APP_KEY DB_PODCAST_REFRESH_TOKEN DB_PODCAST_LIBRARY \
    DB_PODCAST_REMOTE_ROOT DB_PODCAST_FEEDS

# `db-podcast serve`: when to sync, where the subscribe URLs are kept between
# restarts, and who may open the GUI — only Home Assistant's ingress proxy.
DB_PODCAST_SCHEDULE="$(jq -r '(.schedule // []) | join(",")' /data/options.json)"
export DB_PODCAST_SCHEDULE
export DB_PODCAST_URLS="/data/feed-urls.json"
export DB_PODCAST_GUI_FROM="172.30.32.2"
