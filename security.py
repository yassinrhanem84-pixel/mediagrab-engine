"""Security layer for the MediaGrab engine.

The engine runs on a home PC and is reachable from the internet through a tunnel, so it must never be
usable to reach the home network (SSRF), must not be abusable as a free open proxy, and must survive
floods. Everything here is dependency-free so it can't break the engine.
"""
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = Path(__file__).resolve().parent
log = logging.getLogger("mediagrab.security")
_fh = logging.FileHandler(BASE / "security.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
log.setLevel(logging.INFO)


class Blocked(Exception):
    """Request refused for a security reason (message is safe to show)."""


# ------------------------------------------------------------------ client identity
def client_ip(req):
    """Real visitor IP.
    - On the server (PROXY_MODE=caddy): Caddy is the only way in and sets X-Real-IP itself, so only that is trusted
      (a visitor sending a fake CF-Connecting-IP/X-Forwarded-For cannot dodge rate limits).
    - On the home PC behind the Cloudflare tunnel every request comes from 127.0.0.1, so trust CF-Connecting-IP."""
    mode = os.environ.get("PROXY_MODE")
    if mode == "caddy":
        return (req.headers.get("X-Real-IP") or req.remote_addr or "unknown").strip()[:64]
    if mode == "render":  # Render sits behind Cloudflare: that header carries the real visitor, the proxy IP is shared
        return (req.headers.get("CF-Connecting-IP") or (req.headers.get("X-Forwarded-For") or "").split(",")[0]
                or req.remote_addr or "unknown").strip()[:64]
    ip = req.headers.get("CF-Connecting-IP") or ""
    if not ip and req.remote_addr not in ("127.0.0.1", "::1"):
        ip = req.remote_addr or ""
    if not ip:  # direct local use (you on your own PC)
        ip = (req.headers.get("X-Forwarded-For") or req.remote_addr or "local").split(",")[0].strip()
    return ip[:64]


# ------------------------------------------------------------------ SSRF protection
def _ip_is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                                or a.is_reserved or a.is_unspecified)


def check_public_url(url):
    """Only http(s) URLs whose host resolves exclusively to public internet addresses."""
    if not isinstance(url, str) or len(url) > 2048:
        raise Blocked("That link is too long.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise Blocked("Only normal http(s) links are supported.")
    if p.username or p.password:
        raise Blocked("Links with passwords are not supported.")
    host = p.hostname.strip(".").lower()
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".arpa")):
        raise Blocked("That address is not allowed.")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Blocked("That website could not be found. Check the link.")
    ips = {i[4][0] for i in infos}
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise Blocked("That address is not allowed.")
    return url


def safe_get(url, max_redirects=5, **kw):
    """requests.get that re-checks every redirect hop, so a public URL can't bounce into the LAN."""
    kw.setdefault("timeout", 20)
    kw["allow_redirects"] = False
    for _ in range(max_redirects + 1):
        check_public_url(url)
        r = requests.get(url, **kw)
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                raise Blocked("Bad redirect.")
            url = urljoin(url, loc)
            continue
        return r
    raise Blocked("Too many redirects.")


# ------------------------------------------------------------------ input validation
CHOICE_RE = re.compile(r"^(vbest|v\d{2,4}|a_best|a_mp3_(128|192|320))$")
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def valid_choice(c):
    if not isinstance(c, str) or not CHOICE_RE.match(c):
        raise Blocked("Unknown quality option.")
    return c


def valid_token(t):
    return isinstance(t, str) and bool(TOKEN_RE.match(t))


# ------------------------------------------------------------------ rate limiting / flood control
class RateLimiter:
    """Sliding-window limits per (ip, bucket). Thread-safe, memory-bounded."""

    def __init__(self, limits):
        self.limits = limits  # bucket -> (max requests, window seconds)
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, ip, bucket):
        mx, win = self.limits[bucket]
        now = time.time()
        key = (ip, bucket)
        with self.lock:
            q = [t for t in self.hits.get(key, ()) if now - t < win]
            if len(q) >= mx:
                self.hits[key] = q
                return False
            q.append(now)
            self.hits[key] = q
            if len(self.hits) > 50000:  # never let the table grow without bound
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 600}
            return True


LIMITS = RateLimiter({
    "info": (int(os.environ.get("LIMIT_INFO_PER_MIN", "20")), 60),
    "download": (int(os.environ.get("LIMIT_DOWNLOAD_PER_10MIN", "12")), 600),
    "avatar": (int(os.environ.get("LIMIT_AVATAR_PER_MIN", "15")), 60),
    "fetch": (int(os.environ.get("LIMIT_FETCH_PER_MIN", "240")), 60),  # progress polls, previews, files
})


class Slots:
    """Caps how many downloads run at once (whole server and per visitor), so one user can't hog the PC."""

    def __init__(self, total, per_ip):
        self.total, self.per_ip = total, per_ip
        self.active = {}  # task id -> ip
        self.lock = threading.Lock()

    def acquire(self, tid, ip):
        with self.lock:
            if len(self.active) >= self.total:
                raise Blocked("The server is busy right now. Please try again in a minute.")
            if sum(1 for v in self.active.values() if v == ip) >= self.per_ip:
                raise Blocked("Please wait for your current download to finish first.")
            self.active[tid] = ip

    def release(self, tid):
        with self.lock:
            self.active.pop(tid, None)


SLOTS = Slots(int(os.environ.get("MAX_ACTIVE_DOWNLOADS", "4")), int(os.environ.get("MAX_DOWNLOADS_PER_VISITOR", "2")))


# ------------------------------------------------------------------ misc
def scrub(msg):
    """Remove local file paths / user names from error text before it is sent to visitors."""
    msg = str(msg)
    for p in {str(BASE), str(BASE).replace("\\", "\\\\"), str(Path.home()), str(Path.home()).replace("\\", "\\\\")}:
        msg = msg.replace(p, "")
    msg = re.sub(r"[A-Za-z]:\\\\?[^\s'\"]+", "[path]", msg)
    msg = re.sub(r"/(home|Users|tmp|var)/[^\s'\"]+", "[path]", msg)
    return msg[:500]


def disk_ok(path, min_free_gb=float(os.environ.get("MIN_FREE_DISK_GB", "5"))):
    import shutil
    return shutil.disk_usage(path).free > min_free_gb * 1024 ** 3
