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

import spotify
import security as sec
from security import Blocked
from flask import Flask, Response, jsonify, request, send_file

BASE = Path(__file__).resolve().parent
DL_DIR = BASE / "downloads"
DL_DIR.mkdir(exist_ok=True)
COOKIES = BASE / "cookies.txt"  # optional: export from your browser for login-only content

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024  # API requests are tiny JSON bodies
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(4 * 1024 ** 3)))
TASKS = {}  # id -> state dict
PREVIEWS = {}  # token -> (media url, headers, created)

# ---- public-hosting support -------------------------------------------------
# ALLOWED_ORIGINS: comma separated site URLs allowed to call this API (default: any).
ALLOWED = [o.strip().rstrip("/") for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://mediagrab-alpha.vercel.app,http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()]
FILE_TTL = int(os.environ.get("FILE_TTL_SECONDS", "1800"))


def _origin_allowed(origin):
    return "*" in ALLOWED or origin.rstrip("/") in ALLOWED


@app.after_request
def add_headers(resp):
    origin = request.headers.get("Origin", "")
    if origin and _origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; sandbox"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"  # the site plays previews from here
    resp.headers["Cache-Control"] = "no-store"
    resp.headers.pop("Server", None)
    return resp


@app.before_request
def guard():
    if request.method == "OPTIONS":
        return ("", 204)
    if not request.path.startswith("/api/"):
        return None
    ip = sec.client_ip(request)
    request.environ["mg.ip"] = ip
    origin = request.headers.get("Origin", "")
    # browsers always send Origin on cross-site POSTs: refuse other websites piggy-backing on this engine
    if request.method == "POST" and origin and not _origin_allowed(origin):
        sec.log.warning("blocked origin %s from %s %s", origin, ip, request.path)
        return jsonify(error="Not allowed."), 403
    bucket = {"/api/info": "info", "/api/download": "download", "/api/avatar": "avatar"}.get(request.path, "fetch")
    if not sec.LIMITS.allow(ip, bucket):
        sec.log.warning("rate limit %s %s", ip, bucket)
        return jsonify(error="Too many requests - please wait a minute and try again."), 429
    return None


@app.errorhandler(Blocked)
def _blocked(e):
    sec.log.warning("blocked %s %s: %s", request.environ.get("mg.ip"), request.path, e)
    return jsonify(error=str(e)), 400


@app.errorhandler(413)
def _too_big(_e):
    return jsonify(error="Request too large."), 413


@app.errorhandler(500)
def _crash(e):
    sec.log.error("server error on %s: %r", request.path, e)
    return jsonify(error="Something went wrong. Please try again."), 500


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
        for k, v in list(TASKS.items()):
            if v.get("created", time.time()) < cutoff:
                TASKS.pop(k, None)
        for k, v in list(PREVIEWS.items()):
            if v[2] < time.time() - 3600:
                PREVIEWS.pop(k, None)


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
            res = ydl.extract_info(url, download=download)
            if isinstance(res, dict):  # keep the session cookies: some media links (TikTok) need them
                res["_jar"] = {c.name: c.value for c in ydl.cookiejar}
            return res
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
        sec.log.info("youtube bot-check hit - add cookies.txt next to app.py")
        msg = "YouTube is temporarily blocking this server. Please try again later."
    elif "--cookies" in msg or "login" in msg.lower() or "empty media response" in msg:
        msg = "This site requires a login for that post, so it can't be downloaded right now."
    msg = re.sub(r"\s*(Use --cookies|See  https?://\S+|Confirm you are on the latest).*$", "", msg, flags=re.S)
    return sec.scrub(msg)


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


def run_spotify(tid, url, choice, items=None):
    """Each Spotify track is matched to the same song on YouTube and saved as audio."""
    t = TASKS[tid]
    out = DL_DIR / tid
    out.mkdir(exist_ok=True)
    try:
        data = spotify.info(url)
        tracks = data["tracks"]
        picked = [(i, tracks[i - 1]) for i in (items or range(1, len(tracks) + 1)) if 0 < i <= len(tracks)][:PLAYLIST_MAX]
        if not picked:
            raise RuntimeError("No tracks selected.")
        fmt, extra = format_for(choice if choice.startswith("a_") else "a_mp3_320")
        n = len(picked)
        failed = 0
        for pos, (i, tr) in enumerate(picked, 1):
            def hook(d, pos=pos):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    frac = (d.get("downloaded_bytes") or 0) / total if total else 0
                    t.update(status="downloading", percent=round(min(((pos - 1) + frac) / n * 100, 99), 1),
                             item=pos, total=n, speed=human_size(d.get("speed") or 0) + "/s" if d.get("speed") else "")
            name = safe_name(f"{tr['artist']} - {tr['title']}", limit=110)
            prefix = f"{pos:03d} - " if n > 1 else ""
            try:
                extract(spotify.search_query(tr), True, quiet=True, no_warnings=True, format=fmt,
                        outtmpl=str(out / f"{prefix}{name}.%(ext)s"), progress_hooks=[hook], windowsfilenames=True,
                        noplaylist=True, **extra)
            except Exception:
                failed += 1
        files = sorted([p for p in out.iterdir() if p.is_file()], key=lambda p: p.name)
        if not files:
            raise RuntimeError("None of the tracks could be found.")
        if n > 1:
            t.update(status="processing", percent=99)
            zp = out / "bundle.zip"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
                for f in files:
                    z.write(f, f.name)
            t.update(status="done", percent=100, file=str(zp), name=safe_name(data["title"]) + ".zip", count=len(files), failed=failed)
        else:
            t.update(status="done", percent=100, file=str(files[0]), name=files[0].name)
    except Exception as e:  # noqa
        t.update(status="error", error=clean_err(e))


def make_preview(info):
    """Something the browser can play while choosing the part to cut."""
    if (info.get("extractor_key") or "").lower() == "youtube" and info.get("id"):
        return {"type": "youtube", "id": info["id"]}
    cands = [f for f in (info.get("formats") or [info])
             if f.get("url") and f.get("vcodec") != "none" and f.get("acodec") != "none"
             and (f.get("protocol") or "https").startswith("http") and "m3u8" not in (f.get("protocol") or "")
             and (f.get("ext") or "mp4") in ("mp4", "webm", "mov", "m4v")]
    if not cands:
        return None
    f = min(cands, key=lambda f: abs((f.get("height") or 480) - 480))
    headers = dict(f.get("http_headers") or {})
    if f.get("cookies"):  # e.g. TikTok media links only work with the session cookies from the lookup
        attrs = {"domain", "path", "secure", "expires", "httponly", "max-age", "samesite"}
        pairs = [c.strip() for c in f["cookies"].split(";") if "=" in c and c.split("=", 1)[0].strip().lower() not in attrs]
        if pairs:
            headers["Cookie"] = "; ".join(pairs)
    tok = uuid.uuid4().hex
    headers.pop("Cookie", None)
    PREVIEWS[tok] = (f["url"], headers, time.time(), info.get("_jar") or {})
    return {"type": "video", "src": f"/api/stream/{tok}"}


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
    extra_opts = dict(format=fmt, outtmpl=str(out / tmpl), progress_hooks=[hook], windowsfilenames=True,
                      max_filesize=MAX_FILE_BYTES, **extra)
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
    return sec.safe_get(url, headers=h, **kw)


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
    return jsonify(service="MediaGrab engine", status="ok")


def _json():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise Blocked("Bad request.")
    return d


def _clean_url(raw):
    url = (raw or "").strip() if isinstance(raw, str) else ""
    if not url:
        raise Blocked("Paste a link first.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not spotify.is_spotify(url):
        sec.check_public_url(url)
    elif len(url) > 300:
        raise Blocked("That link is too long.")
    return url


def _run_task(fn, tid, *args):
    try:
        fn(tid, *args)
    finally:
        sec.SLOTS.release(tid)


@app.post("/api/info")
def api_info():
    url = _clean_url(_json().get("url"))
    if spotify.is_spotify(url):
        try:
            sp = spotify.info(url)
        except Exception as e:
            return jsonify(error=clean_err(e), url=url), 422
        _, audio = build_choices({"formats": []})
        audio = [a for a in audio if a["id"] != "a_best"] + [a for a in audio if a["id"] == "a_best"]
        if sp["kind"] in ("track", "episode"):
            tr = sp["tracks"][0]
            return jsonify(url=url, title=f"{tr['artist']} - {tr['title']}", uploader=tr["artist"], thumbnail=sp["cover"],
                           duration=int(tr["duration"]) or None, site="Spotify", video=[], audio=audio, spotify=True)
        items = [{"index": i, "title": f"{tr['artist']} - {tr['title']}", "thumbnail": "", "duration": tr["duration"]}
                 for i, tr in enumerate(sp["tracks"][:PLAYLIST_MAX], 1)]
        return jsonify(url=url, is_playlist=True, title=sp["title"], uploader=sp["owner"], thumbnail=sp["cover"],
                       count=len(items), capped=len(sp["tracks"]) > PLAYLIST_MAX, items=items, site="Spotify",
                       video=[], audio=audio, spotify=True)

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
                   site=info.get("extractor_key"), video=video, audio=audio, preview=make_preview(info),
                   playlist_url=f"https://www.youtube.com/playlist?list={pl_id}" if pl_id and "youtu" in q.netloc else None)


@app.post("/api/download")
def api_download():
    d = _json()
    url = _clean_url(d.get("url"))
    choice = sec.valid_choice(d.get("choice", "vbest"))
    raw_items = d.get("items") or []
    if not isinstance(raw_items, list) or len(raw_items) > 1000:
        raise Blocked("Bad selection.")
    items = sorted({int(i) for i in raw_items if str(i).isdigit() and 0 < int(i) <= 10000})[:PLAYLIST_MAX] or None
    try:
        start, end = parse_time(d.get("start")), parse_time(d.get("end"))
    except ValueError:
        return jsonify(error="Times must look like 1:30 or 0:01:30."), 400
    if start is not None and end is not None and end <= start:
        return jsonify(error="The end time must be after the start time."), 400
    if any(v is not None and not (0 <= v <= 48 * 3600) for v in (start, end)):
        raise Blocked("Times must be between 0 and 48 hours.")
    if not sec.disk_ok(DL_DIR):
        sec.log.error("low disk space - refusing downloads")
        raise Blocked("The server is busy right now. Please try again later.")
    tid = uuid.uuid4().hex  # 128-bit: download links can't be guessed
    sec.SLOTS.acquire(tid, request.environ.get("mg.ip", "?"))
    TASKS[tid] = {"status": "starting", "percent": 0, "created": time.time()}
    if spotify.is_spotify(url):
        threading.Thread(target=_run_task, args=(run_spotify, tid, url, choice, items), daemon=True).start()
        return jsonify(id=tid)
    threading.Thread(target=_run_task, args=(run_download, tid, url, choice, bool(d.get("playlist")), items, start, end),
                     daemon=True).start()
    return jsonify(id=tid)


@app.get("/api/stream/<tok>")
def api_stream(tok):
    """Proxies the preview video (with byte ranges, so the player can seek)."""
    p = PREVIEWS.get(tok) if sec.valid_token(tok) else None
    if not p:
        return jsonify(error="Preview expired - get the video again."), 404
    media, hdrs, _, jar = p
    headers = dict(hdrs)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    r = sec.safe_get(media, headers=headers, stream=True, timeout=30, cookies=jar or _cookie_jar())
    ctype = r.headers.get("Content-Type", "video/mp4").split(";")[0].strip()
    if not ctype.startswith(("video/", "audio/", "application/octet-stream", "binary/")):
        r.close()
        return jsonify(error="Preview not available."), 415
    resp = Response(r.iter_content(65536), status=r.status_code, content_type=ctype)
    for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if k in r.headers:
            resp.headers[k] = r.headers[k]
    return resp


@app.get("/api/progress/<tid>")
def api_progress(tid):
    t = TASKS.get(tid) if sec.valid_token(tid) else None
    if not t:
        return jsonify(error="unknown task"), 404
    return jsonify({k: v for k, v in t.items() if k not in ("file", "created")})


@app.get("/api/file/<tid>")
def api_file(tid):
    t = TASKS.get(tid) if sec.valid_token(tid) else None
    if not t or t.get("status") != "done":
        return jsonify(error="not ready"), 404
    f = Path(t["file"]).resolve()
    if DL_DIR.resolve() not in f.parents or not f.is_file():  # never serve anything outside downloads/
        return jsonify(error="not ready"), 404
    return send_file(f, as_attachment=True, download_name=t["name"])


@app.post("/api/avatar")
def api_avatar():
    url = _clean_url(_json().get("url"))
    try:
        img, name = find_avatar(url)
        return jsonify(image=img, name=name)
    except Blocked:
        raise
    except Exception as e:
        return jsonify(error=clean_err(e)), 422


@app.get("/api/avatar-file")
def api_avatar_file():
    """Proxy the image so the browser can save it with a proper filename."""
    img = request.args.get("u", "")
    name = (re.sub(r'[^\w\-. ]', "_", request.args.get("name", "profile"))[:80].strip(". ") or "profile")
    r = _get(img, stream=True)  # SSRF-checked on every redirect hop
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code != 200 or ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"):
        r.close()
        return jsonify(error="That is not an image."), 415
    if int(r.headers.get("Content-Length") or 0) > 20 * 1024 ** 2:
        r.close()
        return jsonify(error="Image too large."), 413
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}[ctype]

    def body(limit=20 * 1024 ** 2):
        sent = 0
        for chunk in r.iter_content(65536):
            sent += len(chunk)
            if sent > limit:
                break
            yield chunk
    resp = app.response_class(body(), mimetype=ctype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}_profile{ext}"'
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"MediaGrab engine running -> http://localhost:{port}")
    try:
        from waitress import serve  # hardened production server (timeouts, connection limits)
        serve(app, host=host, port=port, threads=24, connection_limit=300, channel_timeout=120,
              cleanup_interval=30, ident="", max_request_body_size=64 * 1024)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
