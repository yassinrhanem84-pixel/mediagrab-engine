"""MediaGrab - paste a link, pick a quality, download. Also grabs profile pictures.

Run:  python app.py   then open http://localhost:5000
"""
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

import requests
import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
DL_DIR = BASE / "downloads"
DL_DIR.mkdir(exist_ok=True)
COOKIES = BASE / "cookies.txt"  # optional: export from your browser for login-only content

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

app = Flask(__name__, static_folder=str(BASE / "static"))
TASKS = {}  # id -> state dict

# ---- public-hosting support -------------------------------------------------
# ALLOWED_ORIGINS: comma separated site URLs allowed to call this API (default: any).
ALLOWED = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "30"))
FILE_TTL = int(os.environ.get("FILE_TTL_SECONDS", "1800"))
_hits = {}  # ip -> [timestamps]


@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin", "")
    if "*" in ALLOWED:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin.rstrip("/") in ALLOWED:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp


@app.before_request
def throttle():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.path.startswith("/api/") and request.method == "POST":
        ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()
        now = time.time()
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(recent) >= RATE_PER_MIN:
            return jsonify(error="Too many requests - please wait a minute and try again."), 429
        recent.append(now)
        _hits[ip] = recent


def _cleanup_loop():
    """Delete finished download folders and forget old tasks so the disk never fills up."""
    while True:
        time.sleep(300)
        cutoff = time.time() - FILE_TTL
        for d in DL_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    TASKS.pop(d.name, None)
            except OSError:
                pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.get("/health")
def health():
    return jsonify(ok=True, ffmpeg=shutil.which("ffmpeg") is not None, yt_dlp=yt_dlp.version.__version__)


# ----------------------------------------------------------------- yt-dlp helpers
def base_opts(**extra):
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "http_headers": {"User-Agent": UA},
        # YouTube needs a JS runtime to solve its challenges; node is fine.
        "js_runtimes": {"node": {}, "deno": {}},
        "remote_components": ["ejs:github"],
    }
    if COOKIES.exists():
        o["cookiefile"] = str(COOKIES)
    o.update(extra)
    return o


BROWSERS = ["firefox", "edge", "chrome", "brave", "opera", "vivaldi"]
NEEDS_LOGIN = ("sign in", "confirm you", "cookies", "login", "log in", "rate-limit", "not a bot")


def extract(url, download=False, **opts):
    """Run yt-dlp; if the site demands a login / bot check, retry using cookies from the user's browsers."""
    def run(extra):
        with yt_dlp.YoutubeDL(base_opts(**opts, **extra)) as ydl:
            return ydl.extract_info(url, download=download)
    try:
        return run({})
    except Exception as first:
        if COOKIES.exists() or not any(k in str(first).lower() for k in NEEDS_LOGIN):
            raise
        for b in BROWSERS:
            try:
                return run({"cookiesfrombrowser": (b,)})
            except Exception as e:  # browser missing / cookies unreadable / still blocked
                if not any(k in str(e).lower() for k in NEEDS_LOGIN) and "cookie" not in str(e).lower()                         and "could not" not in str(e).lower() and "not found" not in str(e).lower():
                    raise
        raise first


def clean_err(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = msg.replace("ERROR: ", "").strip()
    if "not a bot" in msg or "Sign in to confirm" in msg:
        msg = ("YouTube is asking for a login / bot check. Fix: export your browser cookies to a file named "
               "cookies.txt (Chrome/Edge/Firefox extension 'Get cookies.txt LOCALLY') and put it next to app.py, "
               "then try again. Original error: " + msg[:160])
    return msg


def human_size(n):
    if not n:
        return ""
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def build_choices(info):
    """Turn yt-dlp's format list into every quality the user can pick."""
    fmts = info.get("formats") or []
    audio_sizes = [f.get("filesize") or f.get("filesize_approx") or 0
                   for f in fmts if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    best_audio_size = max(audio_sizes) if audio_sizes else 0

    heights = {}
    for f in fmts:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        has_audio = f.get("acodec") not in (None, "none")
        cur = heights.get(h)
        fps = f.get("fps") or 0
        total = size + (0 if has_audio else best_audio_size)
        if not cur or fps > cur["fps"] or (fps == cur["fps"] and total > cur["size"]):
            heights[h] = {"height": h, "fps": fps, "size": total}
    video = []
    for h in sorted(heights, reverse=True):
        v = heights[h]
        label = f"{h}p" + (f"{int(v['fps'])}" if v["fps"] and v["fps"] > 30 else "")
        if h >= 4320: label += " (8K)"
        elif h >= 2160: label += " (4K)"
        elif h >= 1440: label += " (2K)"
        video.append({"id": f"v{h}", "label": label, "height": h, "size": human_size(v["size"])})

    audio = [
        {"id": "a_best", "label": "Best audio (original, m4a/opus)", "size": human_size(best_audio_size)},
        {"id": "a_mp3_320", "label": "MP3 320 kbps"},
        {"id": "a_mp3_192", "label": "MP3 192 kbps"},
        {"id": "a_mp3_128", "label": "MP3 128 kbps"},
    ]
    if not video:  # audio-only source or single-file source (e.g. direct mp4)
        if info.get("url") and not best_audio_size:
            video.append({"id": "vbest", "label": "Best available", "height": 0, "size": ""})
    else:
        video.insert(0, {"id": "vbest", "label": "Best available", "height": 0, "size": ""})
    return video, audio


def format_for(choice):
    """Return (yt-dlp format string, extra opts) for a choice id."""
    if choice == "vbest":
        return "bv*+ba/b", {"merge_output_format": "mp4"}
    if choice.startswith("v"):
        h = int(choice[1:])
        return f"bv*[height<={h}]+ba/b[height<={h}]/b", {"merge_output_format": "mp4"}
    if choice == "a_best":
        return "ba/b", {}
    m = re.match(r"a_mp3_(\d+)", choice)
    kbps = m.group(1) if m else "192"
    return "ba/b", {"postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                        "preferredquality": kbps}]}


# ----------------------------------------------------------------- download task
PLAYLIST_MAX = int(os.environ.get("PLAYLIST_MAX", "100"))


def safe_name(s, default="download", limit=120):
    s = re.sub(r"[<>:/|?*]", "_", s or default).replace(chr(34), "_").replace(chr(92), "_")
    return s.strip(". ")[:limit] or default


def parse_time(v):
    """'1:02:03', '2:30', '95' or 95.5 -> seconds (float) / None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(x) for x in str(v).strip().split(":")]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


def hms(sec):
    sec = int(sec)
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}.{m:02d}.{s_:02d}" if h else f"{m}.{s_:02d}"


def run_download(tid, url, choice, playlist=False, items=None, start=None, end=None):
    t = TASKS[tid]
    out = DL_DIR / tid
    out.mkdir(exist_ok=True)
    n_total = len(items) if items else 0

    def hook(d):
        info_d = d.get("info_dict") or {}
        idx = info_d.get("playlist_index") or 1
        n = n_total or info_d.get("n_entries") or 1
        # position of this entry among the selected ones (for progress)
        pos = (items.index(idx) + 1) if items and idx in items else idx
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            pct = ((pos - 1) + frac) / n * 100 if playlist else frac * 100
            t.update(status="downloading", percent=round(min(pct, 99), 1),
                     speed=human_size(d.get("speed") or 0) + "/s" if d.get("speed") else "",
                     eta=f"{d.get('eta')}s" if d.get("eta") else "",
                     item=pos if playlist else None, total=n if playlist else None)
        elif d["status"] == "finished" and not playlist:
            t.update(status="processing", percent=99)

    fmt, extra = format_for(choice)
    tmpl = "%(playlist_index)03d - %(title).70B.%(ext)s" if playlist else "f.%(ext)s"
    extra_opts = dict(format=fmt, outtmpl=str(out / tmpl), progress_hooks=[hook], windowsfilenames=True, **extra)
    clip = not playlist and (start is not None or end is not None)
    if clip:
        # only download/cut the requested part (ffmpeg cuts on exact times)
        extra_opts.update(download_ranges=yt_dlp.utils.download_range_func(None, [(start or 0, end or float("inf"))]),
                          force_keyframes_at_cuts=True)
    if playlist:
        extra_opts.update(noplaylist=False, ignoreerrors=True,
                          playlist_items=",".join(map(str, items)) if items else f"1-{PLAYLIST_MAX}")
    try:
        info = extract(url, True, quiet=True, no_warnings=True, **extra_opts)
        files = sorted([p for p in out.iterdir() if p.is_file()], key=lambda p: p.name)
        if not files:
            raise RuntimeError("Download finished but no file was produced.")
        title = safe_name((info or {}).get("title"))
        if playlist or (len(files) > 1 and (info or {}).get("_type") in ("playlist", "multi_video")):
            # one ZIP = one browser download, so no "allow multiple downloads" prompt
            t.update(status="processing", percent=99)
            zp = out / "bundle.zip"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
                for f in files:
                    z.write(f, f.name)
            t.update(status="done", percent=100, file=str(zp), name=title + ".zip", count=len(files))
        else:
            f = max(files, key=lambda p: p.stat().st_size)
            if clip:
                title = f"{title} [{hms(start or 0)}-{hms(end) if end is not None else 'end'}]"
            name = title + f.suffix if f.stem.startswith("f") else f.name
            t.update(status="done", percent=100, file=str(f), name=name)
    except Exception as e:  # noqa
        t.update(status="error", error=clean_err(e))


# ----------------------------------------------------------------- avatars
def _cookie_jar():
    if COOKIES.exists():
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar(str(COOKIES))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            return jar
        except Exception:
            pass
    return None


def _get(url, **kw):
    kw.setdefault("timeout", 20)
    jar = _cookie_jar()
    if jar is not None and "cookies" not in kw:
        kw["cookies"] = jar
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(kw.pop("headers", {}))
    return requests.get(url, headers=h, **kw)


def _is_image(url):
    try:
        r = _get(url, stream=True)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image")
        r.close()
        return ok
    except Exception:
        return False


def _handle_from_url(url):
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    host = p.netloc.lower().replace("www.", "").replace("m.", "")
    if not segs:
        return host, None
    first = segs[0]
    if host.startswith("youtu"):
        if first.startswith("@"):
            return "youtube", first
        return "youtube", None
    if first in ("p", "reel", "reels", "tv", "stories", "watch", "video", "videos", "share", "i", "status", "photo"):
        return host, None
    return host, first.lstrip("@")


def _youtube_avatar(url, handle):
    target = url
    if handle:
        target = f"https://www.youtube.com/{handle}"
    with yt_dlp.YoutubeDL(base_opts(extract_flat=True, skip_download=True, playlistend=1,
                                    noplaylist=False)) as ydl:
        info = ydl.extract_info(target, download=False)
        # video URL -> go to its channel
        if info.get("_type") != "playlist" and info.get("channel_url"):
            info = ydl.extract_info(info["channel_url"], download=False)
    thumbs = info.get("thumbnails") or []
    best = None
    for t in thumbs:
        if t.get("id") == "avatar_uncropped":
            best = t["url"]
            break
    if not best:
        sq = [t for t in thumbs if t.get("width") and t.get("height") and t["width"] == t["height"]]
        sq.sort(key=lambda t: t["width"], reverse=True)
        if sq:
            best = sq[0]["url"]
    if best:
        best = re.sub(r"=s\d+(-[^?]*)?$", "=s1000", best)  # ask Google for a big version
    return best, info.get("channel") or info.get("uploader") or handle or "youtube"


def _tiktok_avatar(handle):
    html = _get(f"https://www.tiktok.com/@{handle}").text
    for key in ("avatarLarger", "avatarMedium", "avatarThumb"):
        m = re.search(rf'"{key}":"([^"]+)"', html)
        if m:
            return json.loads(f'"{m.group(1)}"')
    return None


def _instagram_avatar(handle):
    try:
        r = _get(f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
                 headers={"X-IG-App-ID": "936619743392459"})
        if r.status_code == 200:
            u = r.json()["data"]["user"]
            return u.get("profile_pic_url_hd") or u.get("profile_pic_url")
    except Exception:
        pass
    try:
        html = _get(f"https://www.instagram.com/{handle}/").text
        m = re.search(r'"profile_pic_url_hd":"([^"]+)"', html) or re.search(r'property="og:image" content="([^"]+)"', html)
        if m:
            return json.loads(f'"{m.group(1)}"') if "\\" in m.group(1) else m.group(1).replace("&amp;", "&")
    except Exception:
        pass
    return None


def _twitter_avatar(handle):
    try:
        r = _get(f"https://api.fxtwitter.com/{handle}")
        if r.status_code == 200:
            a = r.json()["user"]["avatar_url"]
            return a.replace("_normal", "_400x400")
    except Exception:
        pass
    return None


def _facebook_avatar(handle):
    u = f"https://graph.facebook.com/{quote(handle)}/picture?type=large&width=1000&height=1000"
    return u if _is_image(u) else None


def _og_image(url):
    html = _get(url).text
    m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)', html) or \
        re.search(r'content=["\']([^"\']+)["\'] property=["\']og:image', html)
    return m.group(1).replace("&amp;", "&") if m else None


def _unavatar(service, handle):
    u = f"https://unavatar.io/{service}/{quote(handle)}?fallback=false"
    return u if _is_image(u) else None


def _resolve_handle_from_video(url):
    """For post/video links, ask yt-dlp who uploaded it."""
    info = extract(url, False, skip_download=True)
    h = info.get("uploader_id") or info.get("channel_id") or info.get("uploader")
    return (h or "").lstrip("@") or None, info


def find_avatar(url):
    host, handle = _handle_from_url(url)
    plat = ("youtube" if "youtu" in host else "tiktok" if "tiktok" in host else
            "instagram" if "instagram" in host else "twitter" if host in ("twitter.com", "x.com", "t.co") else
            "facebook" if ("facebook" in host or host == "fb.watch") else "other")
    name = handle
    errors = []

    if plat == "youtube":
        try:
            img, name = _youtube_avatar(url, handle)
            if img:
                return img, name
        except Exception as e:
            errors.append(clean_err(e))
    else:
        if not handle:
            try:
                handle, _ = _resolve_handle_from_video(url)
                name = handle
            except Exception as e:
                errors.append(clean_err(e))
        if handle:
            tries = {
                "tiktok": [lambda: _tiktok_avatar(handle), lambda: _unavatar("tiktok", handle)],
                "instagram": [lambda: _instagram_avatar(handle), lambda: _unavatar("instagram", handle)],
                "twitter": [lambda: _twitter_avatar(handle), lambda: _unavatar("twitter", handle)],
                "facebook": [lambda: _facebook_avatar(handle), lambda: _unavatar("facebook", handle)],
                "other": [lambda: _og_image(url)],
            }[plat]
            for fn in tries:
                try:
                    img = fn()
                    if img:
                        return img, name
                except Exception as e:
                    errors.append(clean_err(e))
    if plat == "other" or not errors:
        try:
            img = _og_image(url)
            if img:
                return img, name or host
        except Exception as e:
            errors.append(clean_err(e))
    raise RuntimeError("Could not find a profile picture for this link. " + (errors[0] if errors else ""))


# ----------------------------------------------------------------- routes
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a link first."), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    q = urlparse(url)
    qs = q.query
    # pure playlist links are treated as playlists; "watch?v=..&list=.." stays a single video
    # (but we tell the UI about the playlist so it can offer it)
    is_pl_link = ("list=" in qs and "v=" not in qs) or "/playlist" in q.path or "/sets/" in q.path
    try:
        if is_pl_link:
            info = extract(url, False, skip_download=True, noplaylist=False, extract_flat="in_playlist",
                           playlistend=PLAYLIST_MAX)
        else:
            info = extract(url, False, skip_download=True)
    except Exception as e:
        return jsonify(error=clean_err(e), url=url), 422

    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = [e for e in (info.get("entries") or []) if e]
        items = []
        for i, e in enumerate(entries, 1):
            th = e.get("thumbnail") or ((e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else "")
            items.append({"index": i, "title": e.get("title") or f"Item {i}", "thumbnail": th or "", "duration": e.get("duration")})
        heights = [2160, 1440, 1080, 720, 480, 360, 240, 144]
        video = [{"id": "vbest", "label": "Best available", "height": 0, "size": ""}] + [
            {"id": f"v{h}", "label": f"{h}p", "height": h, "size": ""} for h in heights]
        _, audio = build_choices({"formats": []})
        thumb = info.get("thumbnail") or (items[0]["thumbnail"] if items else "")
        return jsonify(url=url, is_playlist=True, title=info.get("title") or "Playlist",
                       uploader=info.get("uploader") or info.get("channel") or "", thumbnail=thumb, count=len(items),
                       capped=len(items) >= PLAYLIST_MAX, items=items, site=info.get("extractor_key"),
                       video=video, audio=audio)

    video, audio = build_choices(info)
    pl_id = parse_qs(qs).get("list", [None])[0]
    return jsonify(url=url, title=info.get("title") or "Untitled", uploader=info.get("uploader") or info.get("channel") or "",
                   thumbnail=info.get("thumbnail") or "", duration=info.get("duration"),
                   site=info.get("extractor_key"), video=video, audio=audio,
                   playlist_url=f"https://www.youtube.com/playlist?list={pl_id}" if pl_id and "youtu" in q.netloc else None)


@app.post("/api/download")
def api_download():
    d = request.json or {}
    tid = uuid.uuid4().hex[:12]
    TASKS[tid] = {"status": "starting", "percent": 0}
    items = sorted({int(i) for i in d.get("items", []) if str(i).isdigit()})[:PLAYLIST_MAX] or None
    try:
        start, end = parse_time(d.get("start")), parse_time(d.get("end"))
    except ValueError:
        return jsonify(error="Times must look like 1:30 or 0:01:30."), 400
    if start is not None and end is not None and end <= start:
        return jsonify(error="The end time must be after the start time."), 400
    threading.Thread(target=run_download, args=(tid, d["url"], d.get("choice", "vbest"), bool(d.get("playlist")), items, start, end),
                     daemon=True).start()
    return jsonify(id=tid)


@app.get("/api/progress/<tid>")
def api_progress(tid):
    t = TASKS.get(tid)
    if not t:
        return jsonify(error="unknown task"), 404
    return jsonify({k: v for k, v in t.items() if k != "file"})


@app.get("/api/file/<tid>")
def api_file(tid):
    t = TASKS.get(tid)
    if not t or t.get("status") != "done":
        return jsonify(error="not ready"), 404
    return send_file(t["file"], as_attachment=True, download_name=t["name"])


@app.post("/api/avatar")
def api_avatar():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a profile or video link first."), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        img, name = find_avatar(url)
        return jsonify(image=img, name=name)
    except Exception as e:
        return jsonify(error=clean_err(e)), 422


@app.get("/api/avatar-file")
def api_avatar_file():
    """Proxy the image so the browser can save it with a proper filename."""
    img = request.args.get("u", "")
    name = re.sub(r'[^\w\-. ]', "_", request.args.get("name", "profile")) or "profile"
    r = _get(img, stream=True)
    ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
    ext = mimetypes.guess_extension(ctype) or ".jpg"
    ext = ".jpg" if ext == ".jpe" else ext
    resp = app.response_class(r.iter_content(65536), mimetype=ctype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}_profile{ext}"'
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"MediaGrab engine running -> http://localhost:{port}")
    app.run(host=host, port=port, threaded=True)
