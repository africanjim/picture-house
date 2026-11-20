#!/usr/bin/env python3
"""Picture House server: your library as a phone app.

Serves an installable web app (works offline once loaded) with:
  * your Plex library, posters, watched status and TV episodes
  * barcode scanning: scan a DVD or Blu-ray to see if you already have it
  * a wish list of discs you want to buy

  picture-house-server.py --setup      # Plex token + app password (run once)
  picture-house-server.py --password   # change the app password
  picture-house-server.py              # run the server (default port 8765)

Put it behind an HTTPS reverse proxy: phones only allow the camera and
offline mode on https:// addresses. Needs only Python 3. MIT licence.
"""
import argparse
import getpass
import gzip
import hashlib
import hmac
import html
import http.server
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import uuid
import webbrowser

# When packaged as a single program (PyInstaller) the files unpack to sys._MEIPASS.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("picture_house", os.path.join(HERE, "picture-house.py"))
ph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ph)

def _default_data_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Picture House")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Picture House")
    return os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "picture-house-server")


DATA_DIR = os.environ.get("PH_DATA") or _default_data_dir()
PLEX_DEFAULT = os.environ.get("PH_PLEX") or ph.DEFAULT_SERVER
PLEX_TV = "https://plex.tv/api/v2"
APP_DIR = os.path.join(HERE, "app")
SESSION_DAYS = 400
LOOKUP_URL = "https://api.upcitemdb.com/prod/trial/lookup?upc={}"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
CODE_RE = re.compile(r"^\d{6,14}$")
MAX_BODY = 64 * 1024

lock = threading.RLock()


def path(name):
    return os.path.join(DATA_DIR, name)


def read_json(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path(name))


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
    return {"salt": salt, "hash": h}


# ------------------------------------------------------------------ library

class Quiet:
    def step(self, *a):
        pass


class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.body = None        # gzipped JSON
        self.etag = ""
        self.ids = {}           # rating key -> poster version
        self.updated = None
        self.name = None
        self.refreshing = False
        self.error = None
        cached = read_json("library.json", None)
        if cached:
            self._set(cached)

    def _set(self, lib):
        raw = json.dumps(lib, ensure_ascii=False, separators=(",", ":")).encode()
        self.body = gzip.compress(raw, 6)
        self.etag = '"' + hashlib.sha1(raw).hexdigest()[:16] + '"'
        self.ids = {str(i["id"]): i.get("pv", 0) for i in lib["items"] if "id" in i}
        self.updated = lib.get("updated")
        self.name = lib.get("name")

    def refresh(self):
        with lock:
            if self.refreshing:
                return
            self.refreshing = True
        try:
            plex = ph.Plex(self.cfg["server"], self.cfg["token"])
            name, items, eps = ph.read_library(plex, Quiet())
            lib = {"name": name, "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "items": items, "eps": eps}
            write_json("library.json", lib)
            self._set(lib)
            self.error = None
            print(f"Library refreshed: {len(items)} titles", flush=True)
        except Exception as e:  # keep serving the old copy
            self.error = str(e)
            print("Library refresh failed:", e, flush=True)
        finally:
            self.refreshing = False

    def refresh_async(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def loop(self):
        hours = float(self.cfg.get("refresh_hours", 3))
        while True:
            age = time.time() - (os.path.getmtime(path("library.json")) if os.path.exists(path("library.json")) else 0)
            if age >= hours * 3600:
                self.refresh()
            time.sleep(300)


# ----------------------------------------------------------------- handlers

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PictureHouse"
    sys_version = ""
    fails = {}

    def log_message(self, fmt, *args):
        if os.environ.get("PH_LOG"):
            super().log_message(fmt, *args)

    # ---- helpers
    @property
    def cfg(self):
        return self.server.cfg

    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def cookie_flags(self):
        """Secure cookies only over https, so the app also works on plain http at home."""
        https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return "; Secure" if https else ""

    def from_home_network(self):
        """Setup is only allowed from this computer or the home network (the real
        network peer, not a header someone could fake)."""
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_private or ip.is_loopback or ip.is_link_local

    def send(self, code, body=b"", ctype="application/json", headers=None, gz_ok=True):
        if isinstance(body, str):
            body = body.encode()
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        hdrs = dict(headers or {})
        if gz_ok and len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "") \
                and not ctype.startswith("image/"):
            body = gzip.compress(body, 6)
            hdrs["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(self), microphone=()")
        hdrs.setdefault("Cache-Control", "no-cache")
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def session_ok(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ph" and "." in v:
                exp, sig = v.split(".", 1)
                good = hmac.new(self.cfg["secret"].encode(), exp.encode(), "sha256").hexdigest()
                if hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time():
                    return True
        return False

    def static(self, name, ctype, cache="no-cache"):
        try:
            with open(os.path.join(APP_DIR, name), "rb") as f:
                data = f.read()
        except OSError:
            return self.send(404, {"error": "not found"})
        hdrs = {"Cache-Control": cache}
        if name == "index.html":
            data = data.replace(b"__NAME__", html.escape(self.server.library.name or "My").encode())
        if name == "login.html":
            hdrs["X-PH-Login"] = "1"
        self.send(200, data, ctype, hdrs)

    # ---- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        public = {
            "/sw.js": ("sw.js", "text/javascript"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
            "/icon.svg": ("icon.svg", "image/svg+xml"),
            "/icon-192.png": ("icon-192.png", "image/png"),
            "/icon-512.png": ("icon-512.png", "image/png"),
            "/icon-maskable.png": ("icon-maskable.png", "image/png"),
        }
        if p in public:
            return self.static(*public[p])
        if p == "/healthz":
            return self.send(200, {"ok": True})
        if not configured(self.cfg):
            return self.setup_get(p)
        if not self.session_ok():
            if p.startswith("/api/"):
                return self.send(401, {"error": "signed out"})
            return self.static("login.html", "text/html; charset=utf-8")
        if p in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if p == "/api/library":
            lib = self.server.library
            if not lib.body:
                return self.send(503, {"error": "The library hasn't been read from Plex yet. Try again in a minute."})
            if self.headers.get("If-None-Match") == lib.etag:
                return self.send(304, b"", headers={"ETag": lib.etag})
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                return self.send(200, lib.body, headers={"ETag": lib.etag, "Content-Encoding": "gzip"}, gz_ok=False)
            return self.send(200, gzip.decompress(lib.body), headers={"ETag": lib.etag}, gz_ok=False)
        if p == "/api/status":
            lib = self.server.library
            return self.send(200, {"updated": lib.updated, "refreshing": lib.refreshing, "error": lib.error})
        if p == "/api/wishlist":
            return self.send(200, read_json("wishlist.json", []))
        if p == "/api/barcodes":
            return self.send(200, read_json("barcodes.json", {}))
        if p == "/api/checked":
            return self.send(200, read_json("checked.json", {}))
        if p == "/api/lookup":
            return self.lookup(urllib.parse.parse_qs(u.query).get("code", [""])[0])
        m = re.match(r"^/api/poster/(\d+)$", p)
        if m:
            return self.poster(m.group(1))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path
        if not configured(self.cfg):
            return self.setup_post(p)
        if p == "/login":
            return self.login()
        if p == "/logout":
            return self.send(303, b"", headers={"Location": "/", "Set-Cookie": f"ph=; Path=/; Max-Age=0{self.cookie_flags()}; HttpOnly; SameSite=Lax"})
        if not self.session_ok():
            return self.send(401, {"error": "signed out"})
        if p == "/api/refresh":
            self.server.library.refresh_async()
            return self.send(202, {"started": True})
        if p == "/api/barcode":
            return self.save_barcode()
        self.send(404, {"error": "not found"})

    def do_PUT(self):
        p = urllib.parse.urlsplit(self.path).path
        if not self.session_ok():
            return self.send(401, {"error": "signed out"})
        c = re.match(r"^/api/checked/(\d{1,12})$", p)
        if c:
            return self.set_checked(c.group(1), True)
        m = re.match(r"^/api/wishlist/([^/]+)$", p)
        if not m or not ID_RE.match(m.group(1)):
            return self.send(404, {"error": "not found"})
        try:
            raw = self.body_json()
        except ValueError:
            return self.send(400, {"error": "bad request"})
        item = clean_wish(m.group(1), raw)
        if not item:
            return self.send(400, {"error": "A title is needed."})
        with lock:
            wl = read_json("wishlist.json", [])
            wl = [w for w in wl if w.get("id") != item["id"]]
            if len(wl) >= 5000:
                return self.send(400, {"error": "Wish list is full."})
            wl.append(item)
            write_json("wishlist.json", wl)
        self.send(200, item)

    def do_DELETE(self):
        p = urllib.parse.urlsplit(self.path).path
        if not self.session_ok():
            return self.send(401, {"error": "signed out"})
        c = re.match(r"^/api/checked/(\d{1,12})$", p)
        if c:
            return self.set_checked(c.group(1), False)
        m = re.match(r"^/api/wishlist/([^/]+)$", p)
        if not m:
            return self.send(404, {"error": "not found"})
        with lock:
            wl = read_json("wishlist.json", [])
            write_json("wishlist.json", [w for w in wl if w.get("id") != m.group(1)])
        self.send(200, {"ok": True})

    # ---- actions
    def set_checked(self, rid, on):
        """Remember (or forget) that the owner is happy with a flagged title's Plex match."""
        if on and rid not in self.server.library.ids:
            return self.send(404, {"error": "not found"})
        with lock:
            ck = read_json("checked.json", {})
            if on:
                if len(ck) >= 20000:
                    return self.send(400, {"error": "Too many ticks."})
                ck[rid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            else:
                ck.pop(rid, None)
            write_json("checked.json", ck)
        self.send(200, {"ok": True})

    def login(self):
        ip = self.client_ip()
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < 900]
        self.fails[ip] = recent
        if len(recent) >= 8:
            return self.send(429, "Too many tries. Wait 15 minutes and try again.", "text/plain; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode(errors="replace"))
        pw = form.get("password", [""])[0]
        want = self.cfg.get("password") or {}
        ok = bool(want) and hmac.compare_digest(hash_password(pw, want["salt"])["hash"], want["hash"])
        if not ok:
            recent.append(now)
            time.sleep(1)
            return self.send(303, b"", headers={"Location": "/?wrong=1"})
        self.send(303, b"", headers={"Location": "/", "Set-Cookie": self.new_session()})

    def new_session(self):
        exp = str(int(time.time() + SESSION_DAYS * 86400))
        sig = hmac.new(self.cfg["secret"].encode(), exp.encode(), "sha256").hexdigest()
        return f"ph={exp}.{sig}; Path=/; Max-Age={SESSION_DAYS * 86400}{self.cookie_flags()}; HttpOnly; SameSite=Lax"

    # ---- first-run setup in the browser
    def setup_get(self, p):
        if not self.from_home_network():
            return self.send(403, "Picture House isn't set up yet. Open it from a computer on the same "
                                  "home network as the server to finish setup.", "text/plain; charset=utf-8")
        if p == "/api/setup/info":
            return self.send(200, {"plex": PLEX_DEFAULT, "version": ph.VERSION})
        m = re.match(r"^/api/setup/pin/(\d{1,20})$", p)
        if m:
            return self.setup_poll(m.group(1))
        if p.startswith("/api/"):
            return self.send(409, {"error": "not set up yet"})
        return self.static("setup.html", "text/html; charset=utf-8")

    def setup_post(self, p):
        if not self.from_home_network():
            return self.send(403, {"error": "Finish setup from the home network."})
        try:
            b = self.body_json()
        except ValueError:
            return self.send(400, {"error": "bad request"})
        if p == "/api/setup/pin":
            try:
                pin = plex_tv("POST", "/pins?strong=true", self.client_id())
            except PlexTvError as e:
                return self.send(502, {"error": str(e)})
            q = urllib.parse.urlencode({"clientID": self.client_id(), "code": pin["code"],
                                        "context[device][product]": ph.APP})
            return self.send(200, {"id": pin["id"], "url": "https://app.plex.tv/auth#?" + q})
        if p == "/api/setup/find":
            token = str(b.get("token") or "").strip()
            if not token:
                return self.send(400, {"error": "Paste a Plex token first."})
            found = self.discover(token)
            if not found["server"]:
                ok = plex_tv_ok(token, self.client_id())
                return self.send(200, {**found, "error": "Couldn't reach your Plex server from this computer."
                                       if ok else "Plex didn't accept that token. Check you copied all of it."})
            return self.send(200, found)
        if p == "/api/setup/test":
            token = str(b.get("token") or "").strip() or self.server.pending_token
            server = str(b.get("server") or "").strip()
            if not token:
                return self.send(400, {"error": "Sign in with Plex first, or paste a token."})
            found = test_plex(server, token)
            if not found:
                return self.send(200, {"ok": False, "error": "Couldn't reach Plex at that address with that sign-in."})
            return self.send(200, {"ok": True, "server": found[0], "name": found[1]})
        if p == "/api/setup/save":
            token = str(b.get("token") or "").strip() or self.server.pending_token
            pw = str(b.get("password") or "")
            if len(pw) < 8:
                return self.send(400, {"error": "The password needs at least 8 characters."})
            found = test_plex(str(b.get("server") or ""), token) if token else None
            if not found:
                return self.send(400, {"error": "Couldn't reach Plex. Check the address and sign-in."})
            with lock:
                if configured(self.cfg):
                    return self.send(409, {"error": "Already set up."})
                self.cfg.update(server=found[0], token=token, password=hash_password(pw),
                                secret=secrets.token_hex(32))
                write_json("server.json", self.cfg)
            self.server.pending_token = None
            start_library(self.server)
            print(f"Set up: reading {found[1]} at {found[0]}", flush=True)
            return self.send(200, {"ok": True}, headers={"Set-Cookie": self.new_session()})
        self.send(404, {"error": "not found"})

    def client_id(self):
        with lock:
            if not self.cfg.get("client_id"):
                self.cfg["client_id"] = str(uuid.uuid4())
                write_json("server.json", self.cfg)
            return self.cfg["client_id"]

    def setup_poll(self, pin_id):
        try:
            pin = plex_tv("GET", f"/pins/{pin_id}", self.client_id())
        except PlexTvError as e:
            return self.send(502, {"error": str(e)})
        token = pin.get("authToken")
        if not token:
            return self.send(200, {"done": False, "expired": pin.get("expiresIn", 1) <= 0})
        self.server.pending_token = token
        return self.send(200, {"done": True, **self.discover(token)})

    def discover(self, token):
        """Find a working address for the user's Plex server(s). Plex lists every
        address the server has (including Docker-internal ones that only work on
        that machine), so test them all at once, briefly, and keep what answers."""
        cands = [(PLEX_DEFAULT, "", True)]
        try:
            for r in plex_tv("GET", "/resources?includeHttps=1&includeRelay=0", self.client_id(), token):
                if "server" not in (r.get("provides") or "") or not r.get("owned"):
                    continue
                for c in r.get("connections") or []:
                    if c.get("relay"):
                        continue
                    if c.get("address") and c.get("port") and not c.get("IPv6"):
                        cands.append((f"http://{c['address']}:{c['port']}", r.get("name") or "Plex", bool(c.get("local"))))
                    elif c.get("uri"):
                        cands.append((c["uri"], r.get("name") or "Plex", bool(c.get("local"))))
        except PlexTvError:
            pass
        seen, uniq = set(), []
        for c in cands:
            if c[0] not in seen:
                seen.add(c[0])
                uniq.append(c)
        pool = ThreadPoolExecutor(max_workers=min(32, len(uniq)))
        futs = {pool.submit(test_plex, c[0], token, 3): c for c in uniq}
        working, deadline, grace = [], time.time() + 12, None
        pending = set(futs)
        while pending and time.time() < (grace or deadline):
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                uri, name, local = futs[f]
                res = f.result()
                if res:
                    working.append({"uri": res[0], "name": res[1] or name, "local": local,
                                    "rank": uniq.index(futs[f])})
                    if local and not grace:
                        grace = time.time() + 1    # a home address answered: don't wait for the slow ones
        pool.shutdown(wait=False, cancel_futures=True)
        # this computer first, then home-network addresses, then over the internet; one per server
        working.sort(key=lambda w: (w["rank"] != 0, not w["local"], w["rank"]))
        best_per_server = {}
        for w in working:
            best_per_server.setdefault(w["name"], {k: w[k] for k in ("uri", "name", "local")})
        servers = list(best_per_server.values())
        best = servers[0] if servers else None
        return {"server": best["uri"] if best else "", "name": best["name"] if best else "",
                "servers": servers}

    def poster(self, key):
        ver = self.server.library.ids.get(key)
        if ver is None:
            return self.send(404, {"error": "not found"})
        folder = path("posters")
        fn = os.path.join(folder, f"{key}-{ver}.jpg")
        if not os.path.exists(fn):
            plex = ph.Plex(self.cfg["server"], self.cfg["token"])
            q = urllib.parse.urlencode({"width": 240, "height": 360, "minSize": 1, "upscale": 1,
                                        "url": f"/library/metadata/{key}/thumb", "X-Plex-Token": plex.token})
            try:
                with urllib.request.urlopen(f"{plex.server}/photo/:/transcode?{q}", timeout=30, context=plex.ctx) as r:
                    data = r.read()
            except (urllib.error.URLError, OSError):
                return self.send(404, {"error": "no poster"})
            os.makedirs(folder, exist_ok=True)
            for old in os.listdir(folder):
                if old.startswith(key + "-"):
                    os.remove(os.path.join(folder, old))
            with open(fn + ".tmp", "wb") as f:
                f.write(data)
            os.replace(fn + ".tmp", fn)
        with open(fn, "rb") as f:
            self.send(200, f.read(), "image/jpeg", {"Cache-Control": "private, max-age=2592000"})

    def lookup(self, code):
        if not CODE_RE.match(code):
            return self.send(400, {"error": "That doesn't look like a barcode number."})
        known = read_json("barcodes.json", {}).get(code)
        if known:
            return self.send(200, {"found": True, **known})
        req = urllib.request.Request(LOOKUP_URL.format(code), headers={
            "Accept": "application/json", "User-Agent": f"PictureHouse/{ph.VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return self.send(200, {"found": False, "busy": True})
            if e.code in (400, 404):
                return self.send(200, {"found": False})
            return self.send(200, {"found": False, "busy": True})
        except (urllib.error.URLError, OSError, ValueError):
            return self.send(200, {"found": False, "busy": True})
        items = res.get("items") or []
        if not items or not items[0].get("title"):
            return self.send(200, {"found": False})
        entry = {"title": items[0]["title"][:300], "src": "lookup", "at": time.strftime("%Y-%m-%d")}
        with lock:
            codes = read_json("barcodes.json", {})
            codes[code] = entry
            write_json("barcodes.json", codes)
        self.send(200, {"found": True, **entry})

    def save_barcode(self):
        try:
            b = self.body_json()
        except ValueError:
            return self.send(400, {"error": "bad request"})
        code, title = str(b.get("code", "")), str(b.get("title", "")).strip()[:300]
        if not CODE_RE.match(code) or not title:
            return self.send(400, {"error": "bad request"})
        with lock:
            codes = read_json("barcodes.json", {})
            codes[code] = {"title": title, "src": "you", "at": time.strftime("%Y-%m-%d")}
            write_json("barcodes.json", codes)
        self.send(200, {"ok": True})


def configured(cfg):
    return bool(cfg.get("token") and cfg.get("password") and cfg.get("secret"))


class PlexTvError(Exception):
    pass


def plex_tv(method, path, client_id, token=None):
    """Talk to plex.tv for "Sign in with Plex" and the list of your servers."""
    headers = {"Accept": "application/json", "X-Plex-Product": ph.APP, "X-Plex-Version": ph.VERSION,
               "X-Plex-Client-Identifier": client_id, "X-Plex-Device-Name": ph.APP}
    if token:
        headers["X-Plex-Token"] = token
    req = urllib.request.Request(PLEX_TV + path, method=method, headers=headers,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PlexTvError(f"Couldn't reach plex.tv ({e}).") from e


def plex_tv_ok(token, client_id):
    try:
        plex_tv("GET", "/user", client_id, token)
        return True
    except PlexTvError:
        return False


def test_plex(server, token, timeout=8):
    """Return (working address, server name) or None. Tries https too."""
    server = server.strip().rstrip("/")
    if not server or not token:
        return None
    if not re.match(r"^https?://", server):
        cands = ["http://" + server, "https://" + server]
    elif server.startswith("http://"):
        cands = [server, "https://" + server[7:]]
    else:
        cands = [server]
    for c in cands:
        plex = ph.Plex(c, token)
        req = urllib.request.Request(c + "/", headers={"Accept": "application/json", "X-Plex-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=plex.ctx) as r:
                mc = json.load(r).get("MediaContainer", {})
            return c, mc.get("friendlyName") or "Plex"
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def start_library(srv):
    srv.library = Library(srv.cfg)
    threading.Thread(target=srv.library.loop, daemon=True).start()


def clean_wish(wid, raw):
    if not isinstance(raw, dict):
        return None
    s = lambda k, n: str(raw.get(k) or "").strip()[:n]
    title = s("title", 200)
    if not title:
        return None
    item = {"id": wid, "title": title, "added": s("added", 30) or time.strftime("%Y-%m-%d")}
    for k, n in (("code", 14), ("format", 20), ("note", 500), ("raw", 300)):
        if raw.get(k):
            item[k] = s(k, n)
    if str(raw.get("year", "")).isdigit():
        item["year"] = int(raw["year"])
    return item


# --------------------------------------------------------------------- setup

def setup(cfg, password_only=False):
    if not password_only:
        server = input(f"Plex server address [{cfg.get('server') or ph.DEFAULT_SERVER}]: ").strip()
        cfg["server"] = server or cfg.get("server") or ph.DEFAULT_SERVER
        print("\n" + ph.TOKEN_HELP + "\n")
        token = getpass.getpass("Plex token (hidden as you paste, Enter to keep the current one): ").strip()
        if token:
            cfg["token"] = token
        if not cfg.get("token"):
            sys.exit("A Plex token is needed.")
        try:
            ph.Plex(cfg["server"], cfg["token"]).get("/")
        except ph.PlexError as e:
            sys.exit(f"Couldn't read Plex with that address and token ({e}).")
        print("Plex OK.")
    while True:
        pw = getpass.getpass("Choose a password for the app (at least 8 characters): ")
        if len(pw) < 8:
            print("Too short.")
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("They didn't match.")
            continue
        break
    cfg["password"] = hash_password(pw)
    cfg["secret"] = secrets.token_hex(32)   # signs everyone out of old sessions
    write_json("server.json", cfg)
    print(f"Saved in {path('server.json')}. Phones need to sign in again.")


def main():
    ap = argparse.ArgumentParser(description="Serve Picture House as a phone app.")
    ap.add_argument("--setup", action="store_true", help="set the Plex address, token and app password")
    ap.add_argument("--password", action="store_true", help="change the app password")
    ap.add_argument("--host", default=os.environ.get("PH_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PH_PORT", 8765)))
    ap.add_argument("--open", action="store_true", help="open Picture House in your web browser once it's running")
    ap.add_argument("--version", action="version", version=f"{ph.APP} server {ph.VERSION}")
    args = ap.parse_args()
    if getattr(sys, "frozen", False) and not (args.setup or args.password):
        # double-clicked download: open the browser for them (not on a headless server,
        # where Python would fall back to a text-mode browser in the terminal)
        args.open = sys.platform in ("win32", "darwin") or bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    cfg = read_json("server.json", {})
    if args.setup or args.password:
        setup(cfg, password_only=args.password and bool(cfg.get("token")))
        return 0
    try:
        srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"Couldn't start on port {args.port} ({e}). Is Picture House already running?", file=sys.stderr)
        if args.open:
            webbrowser.open(f"http://localhost:{args.port}/")
        return 1
    srv.daemon_threads = True
    srv.cfg = cfg
    srv.pending_token = None
    srv.library = Library(cfg) if configured(cfg) else None
    if srv.library:
        threading.Thread(target=srv.library.loop, daemon=True).start()
    local = f"http://localhost:{args.port}/"
    print(f"Picture House server {ph.VERSION} on http://{args.host}:{args.port}", flush=True)
    if not configured(cfg):
        print(f"Not set up yet: open {local} (or this computer's address, port {args.port}) "
              "in a web browser to finish setup.", flush=True)
    if args.open:
        print("Keep this window open while you use Picture House. Close it to stop.", flush=True)
        threading.Timer(1.0, webbrowser.open, (local,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
