#!/usr/bin/env python3
"""Picture House: turn your Plex library into a browsable, searchable web page.

Reads your Plex Media Server's movie and TV libraries and writes:
  * a single self-contained HTML page (search, genres, decades, directors…)
  * a CSV file for spreadsheets

Runs with double-click pop-ups on an Ubuntu desktop, or in a terminal.
Needs only Python 3 (and zenity for the pop-ups, which Ubuntu includes).

  picture-house                 # pop-up windows if there's a desktop
  picture-house --cli           # terminal only
  picture-house --server http://192.168.1.20:32400
  picture-house --forget-token  # remove the saved token

MIT licence, see LICENSE.
"""
import argparse
import csv
import datetime as dt
import difflib
import getpass
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

APP = "Picture House"
VERSION = "1.2.1"
DEFAULT_SERVER = "http://127.0.0.1:32400"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "picture-house")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PAGE_SIZE = 500

TOKEN_HELP = (
    "Picture House needs your Plex token to read your library.\n\n"
    "To find it:\n"
    "  1. Open Plex in a web browser and go to any movie.\n"
    "  2. Click ⋯ (More), then Get Info, then View XML.\n"
    "  3. In the new tab's address, copy the text after  X-Plex-Token=\n\n"
    "The token stays on this computer and is only sent to your own Plex server."
)


# ----------------------------------------------------------------- UI helpers

class UI:
    """Pop-up windows via zenity when there's a desktop, else the terminal."""

    def __init__(self, gui):
        self.gui = gui
        self.progress = None

    def _zenity(self, *args, input_text=None):
        r = subprocess.run(["zenity", f"--title={APP}", "--width=460", *args],
                           input=input_text, capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def info(self, msg):
        if self.gui:
            self._zenity("--info", f"--text={html.escape(msg)}")
        else:
            print(msg)

    def error(self, msg):
        self.close_progress()
        if self.gui:
            self._zenity("--error", f"--text={html.escape(msg)}")
        else:
            print(f"Error: {msg}", file=sys.stderr)

    def ask_token(self):
        if self.gui:
            code, out = self._zenity("--entry", "--hide-text",
                                     f"--text={html.escape(TOKEN_HELP)}\n\nPlex token:")
            return out if code == 0 else None
        print(TOKEN_HELP + "\n")
        try:
            return getpass.getpass("Plex token (hidden as you paste): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            return None

    def ask_server(self, tried):
        msg = (f"Couldn't reach a Plex server at {tried}.\n\n"
               "If Plex runs on another computer, enter its address, "
               "for example http://192.168.1.20:32400")
        if self.gui:
            code, out = self._zenity("--entry", f"--text={html.escape(msg)}",
                                     f"--entry-text={tried}")
            return out if code == 0 else None
        print(msg)
        try:
            return input("Plex server address (Enter to cancel): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            return None

    def confirm(self, msg):
        if self.gui:
            return self._zenity("--question", f"--text={html.escape(msg)}")[0] == 0
        try:
            return input(f"{msg} [y/N] ").strip().lower().startswith("y")
        except (EOFError, KeyboardInterrupt):
            return False

    def step(self, pct, msg):
        if self.gui:
            if self.progress is None:
                self.progress = subprocess.Popen(
                    ["zenity", "--progress", f"--title={APP}", "--width=460",
                     "--auto-close", "--no-cancel", "--text=Starting…"],
                    stdin=subprocess.PIPE, text=True)
            try:
                self.progress.stdin.write(f"{int(pct)}\n# {msg}\n")
                self.progress.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        else:
            print(f"[{int(pct):3d}%] {msg}")

    def close_progress(self):
        if self.progress:
            try:
                self.progress.stdin.write("100\n")
                self.progress.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            self.progress.wait()
            self.progress = None


# --------------------------------------------------------------- config file

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- Plex access

class PlexError(Exception):
    pass


class Plex:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token
        # Plex's own certificates are issued for *.plex.direct, not the IP you
        # type, so HTTPS to a local address needs verification relaxed.
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path, **params):
        if self.token:
            params["X-Plex-Token"] = self.token
        url = f"{self.server}{path}"
        if params:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "X-Plex-Product": APP, "X-Plex-Version": VERSION,
            "X-Plex-Client-Identifier": "picture-house-script",
        })
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                return json.load(r).get("MediaContainer", {})
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise PlexError("unauthorized") from e
            raise PlexError(f"Plex answered with error {e.code} for {path}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PlexError(f"unreachable: {e}") from e

    def reachable(self):
        try:
            self.get("/identity")
            return True
        except PlexError:
            return False

    def all_items(self, key, type_id):
        items, start = [], 0
        while True:
            mc = self.get(f"/library/sections/{key}/all", type=type_id,
                          **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE_SIZE})
            batch = mc.get("Metadata", [])
            items += batch
            total = mc.get("totalSize", len(items))
            start += len(batch)
            if not batch or start >= total:
                return items


def find_server(ui, server):
    """Try the given address, then https, then ask."""
    while server:
        candidates = [server]
        if server.startswith("http://"):
            candidates.append("https://" + server[len("http://"):])
        elif not server.startswith("https://"):
            candidates = ["http://" + server, "https://" + server]
        for c in candidates:
            if Plex(c, None).reachable():
                return c
        server = ui.ask_server(server)
    return None


# ------------------------------------------------------------ build the data

NORM = re.compile(r"[^a-z0-9]")


def norm(s):
    return NORM.sub("", (s or "").lower())


def title_from_file(path):
    """A readable title and year from a file name like 'The_Thing (1982).mkv'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    year = None
    m = re.search(r"[\(\[][^\)\]\}]*?\b((?:19|20)\d{2})\b[^\)\]\}]*[\)\]\}]", stem)
    if m and int(m.group(1)) <= dt.date.today().year:
        year = int(m.group(1))
        stem = stem[:m.start()] + stem[m.end():]
    return re.sub(r"\s+", " ", stem.replace("_", " ").replace(".", " ")).strip(" -,"), year


def looks_mismatched(file_path, plex_title):
    a = norm(re.sub(r"\(.*?\)|\[.*?\]", "", os.path.splitext(os.path.basename(file_path))[0]))
    b = norm(plex_title)
    if not a or not b:
        return False
    return not (difflib.SequenceMatcher(None, a, b).ratio() >= 0.6 or a in b or b in a)


def build_item(v, library):
    it = {"t": v.get("title", ""), "l": library}
    if str(v.get("ratingKey", "")).isdigit():
        it["id"] = int(v["ratingKey"])
    is_movie = v.get("type") == "movie"
    matched = not v.get("guid", "").startswith(("local://", "com.plexapp.agents.none"))
    file_path = None
    if is_movie:
        try:
            file_path = v["Media"][0]["Part"][0]["file"]
        except (KeyError, IndexError):
            pass
    suspect = bool(matched and file_path and looks_mismatched(file_path, v.get("title")))

    if matched and not suspect:
        if v.get("year"):
            it["y"] = v["year"]
        tags = lambda k, n=None: [x["tag"] for x in v.get(k, [])][:n]
        if tags("Genre"):
            it["g"] = tags("Genre")
        if tags("Director", 2):
            it["d"] = tags("Director", 2)
        rating = v.get("audienceRating") or v.get("rating")
        if rating:
            it["r"] = round(float(rating), 1)
        for src, dst in (("contentRating", "c"), ("studio", "st")):
            if v.get(src):
                it[dst] = v[src]
        if v.get("Country"):
            it["co"] = v["Country"][0]["tag"]
        s = (v.get("summary") or "").strip()
        if s:
            it["s"] = s if len(s) <= 320 else s[:317].rsplit(" ", 1)[0] + "…"
    else:
        if file_path:
            it["t"], y = title_from_file(file_path)
            if y:
                it["y"] = y
        it["x"] = "check" if suspect else "unmatched"
        if suspect:
            it["p"] = f"{v.get('title')} ({v.get('year', '?')})"

    if is_movie:
        if v.get("duration"):
            it["m"] = round(v["duration"] / 60000)
        res = str((v.get("Media") or [{}])[0].get("videoResolution", "")).lower()
        if res:
            it["q"] = {"4k": "4K", "1080": "1080p", "720": "720p"}.get(res, "SD")
        if v.get("viewCount"):
            it["w"] = 1
    else:
        it["tv"] = [v.get("childCount"), v.get("leafCount")]
        it["w"] = v.get("viewedLeafCount", 0)
    if v.get("addedAt"):
        it["a"] = dt.date.fromtimestamp(v["addedAt"]).isoformat()
    thumb = str(v.get("thumb") or "")
    if thumb:
        last = thumb.rstrip("/").rsplit("/", 1)[-1]
        it["pv"] = int(last) if last.isdigit() else 1
    blur = v.get("UltraBlurColors")
    if blur and blur.get("topLeft"):
        it["k"] = [blur.get("topLeft"), blur.get("bottomRight") or blur.get("topLeft")]
    return it


def read_library(plex, ui):
    ui.step(3, "Connecting to Plex…")
    root = plex.get("/")
    name = root.get("friendlyName") or "My Plex"
    sections = [s for s in plex.get("/library/sections").get("Directory", [])
                if s.get("type") in ("movie", "show")]
    if not sections:
        raise PlexError("This Plex server has no movie or TV libraries.")
    items, eps = [], {}
    for i, s in enumerate(sections):
        ui.step(5 + 85 * i / len(sections), f"Reading “{s['title']}”…")
        for v in plex.all_items(s["key"], 1 if s["type"] == "movie" else 2):
            items.append(build_item(v, s["title"]))
        if s["type"] == "show":
            for e in plex.all_items(s["key"], 4):
                show = str(e.get("grandparentRatingKey", ""))
                if not show:
                    continue
                eps.setdefault(show, []).append([
                    e.get("parentIndex") or 0, e.get("index") or 0, e.get("title", ""),
                    1 if e.get("viewCount") else 0, round((e.get("duration") or 0) / 60000)])
    for lst in eps.values():
        lst.sort(key=lambda x: (x[0], x[1]))
    return name, items, eps


# ------------------------------------------------------------------- outputs

def to_js(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def read_checked():
    """Plex ids of flagged titles the owner has already said are fine (optional file)."""
    try:
        with open(os.path.join(CONFIG_DIR, "checked.json"), encoding="utf-8") as f:
            data = json.load(f)
        return sorted(str(k) for k in (data.keys() if isinstance(data, dict) else data))
    except (OSError, ValueError):
        return []


def write_html(path, name, items, eps=None):
    page = (TEMPLATE.replace("__DATA__", to_js(items))
                    .replace("__CHECKED__", to_js(read_checked()))
                    .replace("__EPS__", to_js(eps or {}))
                    .replace("__NAME__", html.escape(name))
                    .replace("__DATE__", dt.date.today().strftime("%-d %b %Y")))
    with open(path, "w", encoding="utf-8") as f:
        f.write("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
                "<meta name=\"color-scheme\" content=\"light dark\">"
                "<style>body{margin:0}[hidden]{display:none!important}</style></head><body>\n")
        f.write(page)
        f.write("\n</body></html>\n")


def write_csv(path, items):
    cols = [("Title", "t"), ("Year", "y"), ("Library", "l"), ("Genres", "g"), ("Director", "d"),
            ("Rating", "r"), ("Age rating", "c"), ("Minutes", "m"), ("Quality", "q"),
            ("Studio", "st"), ("Country", "co"), ("Added", "a"), ("Plex match", "x")]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c for c, _ in cols])
        for it in sorted(items, key=lambda i: i["t"].lower()):
            row = []
            for _, k in cols:
                v = it.get(k, "")
                if k == "x":
                    v = {"check": "check match", "unmatched": "unmatched"}.get(v, "ok")
                row.append(", ".join(v) if isinstance(v, list) else v)
            w.writerow(row)


def output_dir(arg):
    if arg:
        return os.path.expanduser(arg)
    try:
        docs = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True,
                              text=True).stdout.strip()
    except OSError:
        docs = ""
    if not docs or docs == os.path.expanduser("~"):
        docs = os.path.expanduser("~/Documents")
    return os.path.join(docs, APP)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Make a browsable web page of your Plex library.")
    ap.add_argument("--server", help=f"Plex server address (default: saved one, or {DEFAULT_SERVER})")
    ap.add_argument("--token", help="Plex token (or set PLEX_TOKEN). Not saved unless you agree.")
    ap.add_argument("--output", help="folder to save into (default: ~/Documents/Picture House)")
    ap.add_argument("--cli", action="store_true", help="terminal only, no pop-up windows")
    ap.add_argument("--no-open", action="store_true", help="don't open the page when done")
    ap.add_argument("--forget-token", action="store_true", help="delete the saved token and exit")
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    args = ap.parse_args()

    has_desktop = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ui = UI(gui=not args.cli and has_desktop and shutil.which("zenity") is not None)
    cfg = load_config()

    if args.forget_token:
        cfg.pop("token", None)
        save_config(cfg)
        ui.info("Your saved Plex token has been removed.")
        return 0

    server = find_server(ui, args.server or cfg.get("server") or DEFAULT_SERVER)
    if not server:
        ui.error("No Plex server found, so nothing was made.")
        return 1

    token = args.token or os.environ.get("PLEX_TOKEN") or cfg.get("token")
    asked = False
    while True:
        if not token:
            token = ui.ask_token()
            asked = True
            if not token:
                ui.error("No token entered, so nothing was made.")
                return 1
        try:
            name, items, eps = read_library(Plex(server, token), ui)
            break
        except PlexError as e:
            if str(e) == "unauthorized":
                ui.close_progress()
                if cfg.get("token") == token:
                    cfg.pop("token")
                    save_config(cfg)
                ui.error("Plex didn't accept that token. It may be mistyped or signed out. "
                         "Please get a fresh one and try again.")
                token = None
                continue
            ui.error(str(e).replace("unreachable: ", "Lost contact with Plex: "))
            return 1

    cfg["server"] = server
    if asked and ui.confirm("Remember this token so you don't need to enter it next time?\n\n"
                            f"It's saved only on this computer, in {CONFIG_FILE}"):
        cfg["token"] = token
    save_config(cfg)

    ui.step(94, "Writing your page…")
    out = output_dir(args.output)
    os.makedirs(out, exist_ok=True)
    safe = re.sub(r"[^\w\- ]+", "", name).strip() or "Plex"
    page = os.path.join(out, f"{safe} Picture House.html")
    sheet = os.path.join(out, f"{safe} Picture House.csv")
    write_html(page, name, items, eps)
    write_csv(sheet, items)
    ui.step(100, "Done")
    ui.close_progress()

    films = sum(1 for i in items if "tv" not in i)
    shows = len(items) - films
    summary = (f"Made a page for {films:,} films" + (f" and {shows:,} TV shows" if shows else "")
               + f".\n\nSaved in {out}")
    if args.no_open:
        ui.info(summary)
    else:
        if not ui.gui:
            print(summary)
        subprocess.Popen(["xdg-open", page], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


TEMPLATE = r'''<title>__NAME__ Picture House</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#EDEFF2; --surface:#FFFFFF; --sunk:#E3E7EC; --ink:#161B22; --muted:#596371; --faint:#8A94A1;
  --line:#D4DAE1; --accent:#8F5F00; --accent-fill:#F3C24E; --accent-ink:#2A1D00;
  --warn:#9A3412; --warn-fill:#FDE9DD; --focus:#2563EB;
  --display:"Big Shoulders Display","Arial Narrow","Roboto Condensed",sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --ground:#0E1217; --surface:#161B22; --sunk:#1D232C; --ink:#E7EAEE; --muted:#9AA4B1; --faint:#6B7584;
    --line:#29313B; --accent:#F0BE4A; --accent-fill:#F0BE4A; --accent-ink:#1E1500;
    --warn:#FDBA8C; --warn-fill:#3A2116; --focus:#7AA2FF;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#0E1217; --surface:#161B22; --sunk:#1D232C; --ink:#E7EAEE; --muted:#9AA4B1; --faint:#6B7584;
  --line:#29313B; --accent:#F0BE4A; --accent-fill:#F0BE4A; --accent-ink:#1E1500;
  --warn:#FDBA8C; --warn-fill:#3A2116; --focus:#7AA2FF;
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font:15px/1.5 var(--body);padding-inline:16px;padding-block:0 48px}
.wrap{max-width:1080px;margin:0 auto}
header.top{padding-block:36px 20px;display:grid;gap:20px}
.eyebrow{font:500 12px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{font:800 clamp(44px,9vw,84px)/.9 var(--display);letter-spacing:.01em;text-transform:uppercase;margin:6px 0 0;text-wrap:balance}
h1 span{color:var(--accent)}
.stats{display:flex;flex-wrap:wrap;gap:8px 28px;font-variant-numeric:tabular-nums}
.stat b{display:block;font:700 30px/1 var(--display);letter-spacing:.02em}
.stat small{font:400 12px/1.3 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.decades{display:grid;grid-template-columns:repeat(var(--n),1fr);gap:4px;align-items:end;height:92px;padding-top:4px}
.dec{display:flex;flex-direction:column;justify-content:flex-end;align-items:stretch;height:100%;gap:4px;border:0;background:none;padding:0;cursor:pointer;color:inherit;font:inherit}
.dec i{display:block;background:var(--line);border-radius:2px 2px 0 0;min-height:2px;transition:background .15s}
.dec:hover i,.dec:focus-visible i{background:var(--accent-fill)}
.dec span{font:400 10px/1 var(--mono);color:var(--muted);text-align:center}
.dec em{font:500 10px/1 var(--mono);font-style:normal;color:var(--faint);text-align:center}
.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--ground);padding-block:12px;border-bottom:1px solid var(--line);display:grid;gap:10px}
.row1{display:flex;gap:8px;flex-wrap:wrap}
input[type=search],select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}
input[type=search]{flex:1 1 220px}
select{flex:0 1 auto}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{font:700 17px/1 var(--display);letter-spacing:.04em;text-transform:uppercase;border:1px solid transparent;background:none;color:var(--muted);padding:8px 12px;border-radius:999px;cursor:pointer;white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab[aria-selected=true]{background:var(--ink);color:var(--ground)}
.tab .n{font:500 11px/1 var(--mono);margin-left:6px;opacity:.7}
.jump{display:flex;flex-wrap:wrap;gap:4px;padding-block:14px 4px}
.jump a{font:500 12px/1 var(--mono);color:var(--muted);text-decoration:none;padding:6px 8px;border-radius:6px;background:var(--sunk)}
.jump a:hover{color:var(--accent-ink);background:var(--accent-fill)}
.note{color:var(--muted);font-size:14px;max-width:65ch;margin:14px 0 0}
.group{padding-top:26px;scroll-margin-top:130px}
.group h2{font:800 32px/1 var(--display);text-transform:uppercase;letter-spacing:.02em;margin:0 0 8px;display:flex;align-items:baseline;gap:10px;text-wrap:balance}
.group h2 small{font:500 12px/1 var(--mono);color:var(--muted);letter-spacing:.06em}
ul.films{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
li.f{border-top:1px solid var(--line)}
li.f:first-child{border-top:0}
li.f>button{all:unset;box-sizing:border-box;width:100%;display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:center;padding:9px 12px;cursor:pointer}
li.f>button:hover{background:var(--sunk)}
li.f>button:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.chip{width:34px;height:48px;border-radius:4px;background:var(--sunk);box-shadow:inset 0 0 0 1px rgba(127,127,127,.18)}
.ti{min-width:0}
.ti b{font-weight:600}
.ti .yr{font:400 13px var(--mono);color:var(--muted);margin-left:6px}
.ti .sub{display:block;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{display:flex;gap:10px;align-items:center;font:400 12px/1 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.q{font:500 10px/1 var(--mono);letter-spacing:.06em;padding:4px 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.q.k4{background:var(--accent-fill);color:var(--accent-ink);border-color:transparent}
.flag{font:500 10px/1 var(--mono);letter-spacing:.04em;text-transform:uppercase;padding:4px 6px;border-radius:4px;background:var(--warn-fill);color:var(--warn)}
.more{padding:0 12px 14px 58px;display:grid;gap:8px;color:var(--ink);font-size:14px}
.more p{margin:0;max-width:65ch}
.more dl{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0;font-size:13px}
.more dt{font:500 11px var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
.more dd{margin:0 0 0 6px}
.more dl div{display:flex;align-items:baseline}
.seen{font:500 10px/1 var(--mono);letter-spacing:.04em;text-transform:uppercase;padding:4px 6px;border-radius:4px;background:var(--sunk);color:var(--muted)}
.seen.part{text-transform:none}
.season{border-top:1px solid var(--line);padding-top:6px}
.season summary{cursor:pointer;font-weight:600}
.season summary small{font:400 12px var(--mono);color:var(--muted);margin-left:6px}
ol.eps{list-style:none;margin:6px 0 4px;padding:0;display:grid;gap:2px;font-size:13px}
ol.eps li{display:grid;grid-template-columns:28px 1fr auto;gap:8px;padding:3px 0}
ol.eps li.w{color:var(--muted)}
.en,.em{font:400 12px var(--mono);color:var(--faint);font-variant-numeric:tabular-nums}
.empty{padding:40px 0;color:var(--muted);text-align:center}
li.f.has-ck{display:grid;grid-template-columns:auto 1fr}
li.f.has-ck>.more{grid-column:1/-1}
label.ck{display:flex;align-items:center;justify-content:center;padding:0 2px 0 12px;cursor:pointer}
label.ck input{width:20px;height:20px;accent-color:var(--accent);cursor:pointer;margin:0}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--ground);padding:10px 16px;border-radius:10px;font-size:14px;z-index:30;max-width:calc(100% - 32px);box-shadow:0 6px 24px rgba(0,0,0,.25)}
.toast button{all:unset;margin-left:12px;font-weight:700;text-decoration:underline;cursor:pointer}
.hide-sm{}
@media (max-width:620px){
  .hide-sm{display:none}
  li.f>button{grid-template-columns:28px 1fr auto;gap:10px;padding:8px 10px}
  .chip{width:28px;height:40px}
  .more{padding-left:48px}
  .decades{height:72px}
  .dec em{display:none}
}
footer{color:var(--faint);font:400 12px/1.5 var(--mono);padding-top:40px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <div class="eyebrow">Plex server __NAME__ · updated __DATE__</div>
      <h1>__NAME__ <span>Picture House</span></h1>
    </div>
    <div class="stats" id="stats"></div>
    <div class="decades" id="decades" aria-label="Films by decade"></div>
  </header>

  <div class="bar">
    <div class="row1">
      <input type="search" id="q" placeholder="Search titles, directors, genres…" aria-label="Search">
      <select id="lib" aria-label="Library"><option value="">All libraries</option></select>
      <select id="qual" aria-label="Quality">
        <option value="">Any quality</option><option>4K</option><option>1080p</option><option>720p</option><option>SD</option>
      </select>
      <select id="seen" aria-label="Watched">
        <option value="">Watched or not</option><option value="n">Not watched</option><option value="y">Watched</option>
      </select>
      <select id="sort" aria-label="Sort">
        <option value="t">Sort: title</option><option value="y">Sort: newest year</option><option value="yo">Sort: oldest year</option><option value="r">Sort: rating</option><option value="a">Sort: recently added</option><option value="m">Sort: longest</option>
      </select>
    </div>
    <div class="tabs" role="tablist" id="tabs"></div>
  </div>

  <main id="main"></main>
  <footer>Details and genres come from Plex's online matches. Made with Picture House. Titles marked <span class="flag">unmatched</span> or <span class="flag">check match</span> use the file name instead.</footer>
</div>

<script>
const DATA = __DATA__;
const EPS = __EPS__;
/* titles you've ticked as fine leave "Needs attention" for good (saved in this browser) */
const PRECHECKED = new Set(__CHECKED__);
let TICKED = new Set(); try { TICKED = new Set(JSON.parse(localStorage.getItem("ph-checked") || "[]")); } catch (e) {}
const isChecked = d => PRECHECKED.has(String(d.rk)) || TICKED.has(String(d.rk));
function applyChecked() { DATA.forEach(d => { if (d.x0 === undefined) d.x0 = d.x || ""; d.x = d.x0 && !isChecked(d) ? d.x0 : ""; }); }
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sortKey = t => t.toLowerCase().replace(/^(the|a|an)\s+/, "").replace(/^[^a-z0-9]+/, "");
DATA.forEach((d, i) => { d.rk = d.id; d.id = i; d.sk = sortKey(d.t); d.hay = [d.t, d.y, ...(d.g||[]), ...(d.d||[]), d.l, d.st, d.co].join(" ").toLowerCase(); });
applyChecked();

const VIEWS = [
  {id:"az", name:"A–Z"},
  {id:"genre", name:"Genre"},
  {id:"decade", name:"Decade"},
  {id:"library", name:"Library"},
  {id:"director", name:"Director"},
  {id:"added", name:"Recently added"},
  {id:"attention", name:"Needs attention"},
];
let state = { view: "az" };
try { const v = localStorage.getItem("ph-view"); if (VIEWS.some(x => x.id === v)) state.view = v; } catch(e){}
const h = location.hash.slice(1); if (VIEWS.some(x => x.id === h)) state.view = h;

/* header stats */
const films = DATA.filter(d => !d.tv), shows = DATA.filter(d => d.tv);
const hours = Math.round(films.reduce((s, d) => s + (d.m || 0), 0) / 60);
const yrs = films.map(d => d.y).filter(Boolean);
const eps = shows.reduce((s, d) => s + (d.tv[1] || 0), 0);
$("#stats").innerHTML = [
  [films.length.toLocaleString(), "films"],
  ...(shows.length ? [[shows.length, `TV shows · ${eps} episodes`]] : []),
  [hours.toLocaleString(), "hours of film"],
  ...(yrs.length ? [[`${Math.min(...yrs)}–${Math.max(...yrs)}`, "years spanned"]] : []),
  ...(films.some(d => d.q === "4K") ? [[films.filter(d => d.q === "4K").length, "in 4K"]] : []),
].map(([b, s]) => `<div class="stat"><b>${b}</b><small>${s}</small></div>`).join("");

/* decade histogram, one scale */
const decOf = y => Math.floor(y / 10) * 10;
const decCounts = {};
yrs.forEach(y => decCounts[decOf(y)] = (decCounts[decOf(y)] || 0) + 1);
const decKeys = []; if (yrs.length) for (let d = decOf(Math.min(...yrs)); d <= decOf(Math.max(...yrs)); d += 10) decKeys.push(d);
const decMax = Math.max(1, ...Object.values(decCounts));
if (!decKeys.length) $("#decades").hidden = true;
const dh = $("#decades"); dh.style.setProperty("--n", decKeys.length);
dh.innerHTML = decKeys.map(d => {
  const n = decCounts[d] || 0;
  return `<button class="dec" data-dec="${d}" title="${n} films from the ${d}s"><em>${n || ""}</em><i style="height:${(n / decMax * 100).toFixed(1)}%"></i><span>${String(d).slice(2)}s</span></button>`;
}).join("");
dh.addEventListener("click", e => { const b = e.target.closest(".dec"); if (!b) return; setView("decade"); requestAnimationFrame(() => document.getElementById("g-" + b.dataset.dec)?.scrollIntoView({behavior:"smooth"})); });

/* filters */
[...new Set(DATA.map(d => d.l))].sort().forEach(l => $("#lib").insertAdjacentHTML("beforeend", `<option>${esc(l)}</option>`));
["q","lib","qual","seen","sort"].forEach(id => $("#" + id).addEventListener("input", render));

function tabs(counts) {
  $("#tabs").innerHTML = VIEWS.map(v => `<button class="tab" role="tab" data-v="${v.id}" aria-selected="${v.id === state.view}">${v.name}${v.id === "attention" ? `<span class="n">${counts.attention}</span>` : ""}</button>`).join("");
}
$("#tabs").addEventListener("click", e => { const b = e.target.closest(".tab"); if (b) setView(b.dataset.v); });
function setView(v) { state.view = v; try { localStorage.setItem("ph-view", v); } catch(e){} history.replaceState(null, "", "#" + v); render(); }

const isSeen = d => d.tv ? d.w >= (d.tv[1] || 1) : !!d.w;
const sorters = {
  t: (a, b) => a.sk.localeCompare(b.sk),
  y: (a, b) => (b.y || 0) - (a.y || 0) || a.sk.localeCompare(b.sk),
  yo: (a, b) => (a.y || 9999) - (b.y || 9999) || a.sk.localeCompare(b.sk),
  r: (a, b) => (b.r || 0) - (a.r || 0) || a.sk.localeCompare(b.sk),
  a: (a, b) => (b.a || "").localeCompare(a.a || ""),
  m: (a, b) => (b.m || 0) - (a.m || 0),
};

function row(d, ck) {
  const [c1, c2] = d.k || [];
  const chip = c1 ? `style="background:linear-gradient(160deg,#${c1},#${c2 || c1})"` : "";
  const sub = d.x === "check" ? `Plex matched this to “${esc(d.p)}”` :
              d.x === "unmatched" ? `Not matched in Plex · ${esc(d.l)}` :
              [ (d.g || []).slice(0, 3).join(", "), d.d ? d.d.join(" & ") : "", d.tv ? `${d.tv[0]} season${d.tv[0] === 1 ? "" : "s"} · ${d.tv[1]} episodes` : "" ].filter(Boolean).join(" · ");
  const meta = [
    d.x ? `<span class="flag">${d.x === "check" ? "check match" : "unmatched"}</span>` : "",
    d.m ? `<span class="hide-sm">${Math.floor(d.m / 60)}h ${String(d.m % 60).padStart(2, "0")}m</span>` : "",
    isSeen(d) ? `<span class="seen" title="Watched">✓ seen</span>` : d.tv && d.w ? `<span class="seen part" title="Episodes watched">${d.w}/${d.tv[1]}</span>` : "",
    d.r ? `<span>★ ${d.r.toFixed(1)}</span>` : "",
    d.q ? `<span class="q ${d.q === "4K" ? "k4" : ""} hide-sm">${d.q}</span>` : "",
  ].join("");
  const box = ck && d.rk ? `<label class="ck" title="Tick when you're happy with this one"><input type="checkbox" data-ok="${d.rk}" aria-label="Happy with ${esc(d.t)}"></label>` : "";
  return `<li class="f${box ? " has-ck" : ""}">${box}<button aria-expanded="false" data-id="${d.id}"><span class="chip" ${chip}></span><span class="ti"><b>${esc(d.t)}</b>${d.y ? `<span class="yr">${d.y}</span>` : ""}<span class="sub">${sub}</span></span><span class="meta">${meta}</span></button></li>`;
}
function detail(d) {
  const f = [["Library", d.l], ["Rated", d.c], ["Studio", d.st], ["Country", d.co], ["Director", d.d?.join(", ")], ["Genres", d.g?.join(", ")], ["Quality", d.q], ["Added", d.a]].filter(x => x[1]);
  const note = d.x === "check" ? `<p>The file name suggests a different film from Plex's match. To fix it in Plex, open the title, choose ⋯ then <b>Fix Match</b>.</p>` :
               d.x === "unmatched" ? `<p>Plex has no online details for this one, so there's no genre, rating or summary.</p>` : "";
  const ep = d.tv && EPS[d.rk] ? episodes(EPS[d.rk]) : "";
  return `<div class="more">${d.s ? `<p>${esc(d.s)}</p>` : ""}${note}${ep}<dl>${f.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl></div>`;
}
function episodes(list) {
  const by = new Map(); list.forEach(e => { if (!by.has(e[0])) by.set(e[0], []); by.get(e[0]).push(e); });
  return [...by].map(([sn, eps]) => `<details class="season"><summary>${sn ? "Season " + sn : "Specials"} <small>${eps.filter(e => e[3]).length}/${eps.length} watched</small></summary><ol class="eps">${eps.map(e =>
    `<li class="${e[3] ? "w" : ""}"><span class="en">${e[1]}</span><span>${esc(e[2])}</span><span class="em">${e[3] ? "✓" : ""}${e[4] ? " " + e[4] + "m" : ""}</span></li>`).join("")}</ol></details>`).join("");
}
$("#main").addEventListener("click", e => {
  const b = e.target.closest("li.f>button"); if (!b || e.target.closest(".more")) return;
  const open = b.getAttribute("aria-expanded") === "true";
  b.setAttribute("aria-expanded", !open);
  if (open) b.nextElementSibling?.remove(); else b.insertAdjacentHTML("afterend", detail(DATA[b.dataset.id]));
});

function groupsFor(list, view) {
  const g = new Map(); const add = (k, d) => { if (!g.has(k)) g.set(k, []); g.get(k).push(d); };
  if (view === "az") list.forEach(d => { const c = d.sk[0] || "#"; add(/[a-z]/.test(c) ? c.toUpperCase() : "#", d); });
  if (view === "genre") list.forEach(d => (d.g && d.g.length ? d.g : ["No genre listed"]).forEach(x => add(x, d)));
  if (view === "decade") list.forEach(d => add(d.y ? decOf(d.y) + "s" : "Year unknown", d));
  if (view === "library") list.forEach(d => add(d.l, d));
  if (view === "director") list.forEach(d => (d.d && d.d.length ? d.d : ["Director unknown"]).forEach(x => add(x, d)));
  if (view === "added") list.forEach(d => add(d.a ? new Date(d.a + "T00:00").toLocaleDateString(undefined, {month:"long", year:"numeric"}) : "Date unknown", d));
  if (view === "attention") list.filter(d => d.x).forEach(d => add(d.x === "check" ? "Check the Plex match" : "Not matched in Plex", d));
  let keys = [...g.keys()];
  const last = k => /unknown|No genre/.test(k) ? 1 : 0;
  if (view === "az") keys.sort((a, b) => (a === "#") - (b === "#") || a.localeCompare(b));
  else if (view === "decade") keys.sort((a, b) => last(a) - last(b) || parseInt(b) - parseInt(a));
  else if (view === "added") keys.sort((a, b) => last(a) - last(b) || (g.get(b)[0].a || "").localeCompare(g.get(a)[0].a || ""));
  else if (view === "genre" || view === "director") keys.sort((a, b) => last(a) - last(b) || g.get(b).length - g.get(a).length || a.localeCompare(b));
  else keys.sort((a, b) => a.localeCompare(b));
  if (view === "director") keys = keys.filter(k => g.get(k).length > 1 || state.q);
  return keys.map(k => [k, g.get(k)]);
}

function render() {
  const q = $("#q").value.trim().toLowerCase(), lib = $("#lib").value, qual = $("#qual").value, seen = $("#seen").value;
  state.q = q;
  const terms = q.split(/\s+/).filter(Boolean);
  const list = DATA.filter(d => (!lib || d.l === lib) && (!qual || d.q === qual) && (!seen || (seen === "y") === isSeen(d)) && terms.every(t => d.hay.includes(t)));
  const counts = { attention: DATA.filter(d => d.x).length };
  tabs(counts);
  const sorter = sorters[$("#sort").value] || sorters.t;
  const view = state.view === "added" ? "added" : state.view;
  const groups = groupsFor(list, view);
  groups.forEach(([, items]) => items.sort(state.view === "added" ? sorters.a : sorter));
  const slug = k => "g-" + String(k).replace(/s$/, "").replace(/[^a-z0-9]+/gi, "-");
  const jump = groups.length > 3 && view !== "added" ? `<nav class="jump" aria-label="Jump to">${groups.map(([k]) => `<a href="#${slug(k)}">${esc(k)}</a>`).join("")}</nav>` : "";
  const notes = {
    genre: "A film with several genres appears under each one.",
    director: "Directors with two or more films here. Search a name to see anyone with a single film.",
    attention: "Unmatched titles have no online details in Plex. “Check match” titles look like Plex matched the wrong film. Fix either in Plex with ⋯ › Fix Match. Tick the box once you're happy with a title and it leaves this list for good.",
  };
  $("#main").innerHTML = (notes[view] ? `<p class="note">${notes[view]}</p>` : "") + jump + (groups.length ? groups.map(([k, items]) =>
    `<section class="group" id="${slug(k)}"><h2>${esc(k)}<small>${items.length}</small></h2><ul class="films">${items.map(d => row(d, view === "attention")).join("")}</ul></section>`).join("")
    : view === "attention" && !q ? `<p class="empty">Nothing left to check. Nice work.</p>` : `<p class="empty">Nothing matches “${esc(q)}”. Try fewer words or clear the filters.</p>`);
  $("#main").querySelectorAll(".jump a").forEach(a => a.addEventListener("click", e => { e.preventDefault(); document.getElementById(a.getAttribute("href").slice(1))?.scrollIntoView({behavior:"smooth"}); }));
}
render();

function setChecked(rk, on) {
  rk = String(rk); on ? TICKED.add(rk) : TICKED.delete(rk);
  try { localStorage.setItem("ph-checked", JSON.stringify([...TICKED])); } catch (e) {}
  applyChecked(); render();
}
let toastTimer;
function toast(msg, undo) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
  t.textContent = msg; t.hidden = false; clearTimeout(toastTimer);
  if (undo) { const b = document.createElement("button"); b.type = "button"; b.textContent = "Undo"; b.onclick = () => { t.hidden = true; undo(); }; t.append(b); }
  toastTimer = setTimeout(() => t.hidden = true, 6000);
}
$("#main").addEventListener("change", e => {
  const cb = e.target.closest("label.ck input"); if (!cb) return;
  const d = DATA.find(x => String(x.rk) === cb.dataset.ok); if (!d) return;
  setChecked(d.rk, true);
  toast(`${d.t} is off the list`, () => setChecked(d.rk, false));
});
</script>
'''

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
