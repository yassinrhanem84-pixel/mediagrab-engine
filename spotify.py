"""Spotify links -> track names. Spotify audio itself is DRM-protected, so (like spotDL) each track
is matched to the same song on YouTube and downloaded from there."""
import json
import re

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_CACHE = {}


def is_spotify(url):
    return "open.spotify.com/" in url or url.startswith("spotify:")


def parse(url):
    m = re.search(r"(track|album|playlist|episode)[/:]([A-Za-z0-9]{10,})", url)
    if not m:
        raise ValueError("Paste a Spotify track, album or playlist link.")
    return m.group(1), m.group(2)


def info(url):
    kind, sid = parse(url)
    key = (kind, sid)
    if key in _CACHE:
        return _CACHE[key]
    html = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": UA}, timeout=20).text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("Could not read this Spotify link. Is it public?")
    ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    cover = ""
    try:
        cover = requests.get("https://open.spotify.com/oembed", params={"url": f"https://open.spotify.com/{kind}/{sid}"},
                             headers={"User-Agent": UA}, timeout=10).json().get("thumbnail_url", "")
    except Exception:
        pass
    title = ent.get("name") or ent.get("title") or "Spotify"
    if kind in ("track", "episode"):
        artists = ", ".join(a.get("name", "") for a in ent.get("artists") or []) or ent.get("subtitle", "")
        tracks = [{"title": title, "artist": artists, "duration": (ent.get("duration") or 0) / 1000}]
        owner = artists
    else:
        tracks = [{"title": t.get("title", ""), "artist": (t.get("subtitle") or "").replace("\xa0", " "),
                   "duration": (t.get("duration") or 0) / 1000} for t in ent.get("trackList") or []]
        owner = (ent.get("subtitle") or "").replace("\xa0", " ")
    data = {"kind": kind, "title": title, "owner": owner, "cover": cover, "tracks": tracks}
    _CACHE[key] = data
    return data


def search_query(track):
    """yt-dlp search that prefers the official audio of the same song."""
    return f"ytsearch1:{track['artist']} - {track['title']} audio"
