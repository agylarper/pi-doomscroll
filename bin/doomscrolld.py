#!/usr/bin/env python3
"""doomscrolld: the feed, downloader and player behind the Doomscroll Claude Code mod.

The mod spawns this once per session and talks to it two ways.

stdout, one line per event:
    R <socket>                                   ready; commands go to <socket>
    F <shm|file> <name> <gen> <w> <h> <pos> <dur> a decoded RGB frame to blit
    S <json>                                     the feed / player state changed
    E <text>                                     something went wrong

socket: HTTP over a Unix socket, `POST /cmd` with a JSON body `{"op": ...}`,
answered with the state as JSON. Ops: play, pause, next, prev, mute, size,
creators, open, state, doctor.

Needs python3 (3.8+), ffmpeg/ffprobe and yt-dlp on PATH. macOS and Linux;
on Windows it only says so.

Frames are written as POSIX shared-memory objects the terminal reads and
unlinks itself (kitty graphics, `t=s`), so no pixel crosses the mod. A paused
frame is also kept as a plain file (`poster`) so a redraw can show it again.
"""

import argparse
import json
import mmap
import os
import random
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler

try:
    import _posixshmem
except ImportError:  # no POSIX shm: fall back to a ring of files
    _posixshmem = None
try:
    import fcntl
    import termios
except ImportError:  # Windows
    fcntl = termios = None

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"

FPS = 30
MAX_FRAME_H = 800
INDEX_TTL = 6 * 3600
PER_CREATOR = 15
MAX_DURATION = 180
MIX_SHARE = 0.3  # of each category, sampled fresh every session
CACHED_MAX_AGE = 7 * 24 * 3600  # a cached creator joins the mix free for a week

# The For You mix: creators verified with yt-dlp in October 2026, by flavour.
# Each session samples from every category, so the feed swings between
# comedy, memes, slop, animals, sports and the rest like a real For You page.
POOL = {
    "comedy and memes": [
        "khaby.lame", "zachking", "kingbach", "drewafualo", "alanchikinchow", "jaboukie",
        "daquan", "memes", "memezar", "9gag", "dailydoseofinternet", "failarmy", "pubity",
        "unilad", "ladbible", "theonion", "brainrotreels",
    ],
    "streamers and creators": [
        "mrbeast", "ishowspeed", "adinross", "kaicenatclips", "ksi", "sidemen", "dudeperfect",
        "spencerx", "bellapoarch", "charlidamelio", "addisonre", "noahbeck",
    ],
    "food": [
        "bdylanhollis", "cznburak", "nickdigiovanni", "mrnigelng", "gordonramsayofficial",
        "tasty", "cookingwithlynja",
    ],
    "slop and satisfying": [
        "blossom", "crafty.panda", "troomtroom", "oddlysatisfying", "slime", "subwaysurfers",
        "minecraft", "minecraftparkour",
    ],
    "animals": ["thedodo", "tuckerbudzyn", "weratedogs", "dogsoftiktok", "animalsdoingthings"],
    "sports": [
        "nba", "f1", "premierleague", "overtime", "houseofhighlights", "espn", "bleacherreport",
        "brfootball", "barstoolsports",
    ],
    "unhinged brands": ["duolingo", "ryanair", "scrubdaddy", "chipotle", "mcdonalds", "wendys"],
    "tv and celebs": [
        "theoffice", "spongebob", "shrek", "snl", "fallontonight", "jimmyfallon", "willsmith",
        "therock", "mkbhd", "mrwhosetheboss", "nasa",
    ],
}


def sample_pool():
    picked = []
    for handles in POOL.values():
        count = max(1, round(len(handles) * MIX_SHARE))
        picked += random.sample(handles, count)
    random.shuffle(picked)
    return picked
AHEAD = 4
SLIDE_FRAMES = 12
SLIDE_MS = 200
PREROLL_KEEP = 16
KEEP_VIDEOS = 40
SHM_KEEP = 90
FILE_RING = 16
DEFAULT_CELL = (8.0, 17.0)

# Where Homebrew, pipx and pip --user put yt-dlp and ffmpeg, in case the
# session's PATH lacks them.
for extra in ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")):
    if extra not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")

_out_lock = threading.Lock()
DAEMON = None
RECENT_ERRORS = deque(maxlen=12)


def emit(line):
    if line.startswith("E "):
        RECENT_ERRORS.append(time.strftime("%H:%M:%S ") + line[2:])
    try:
        with _out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        die()


def die(*_):
    try:
        if DAEMON:
            DAEMON.shutdown()
    finally:
        os._exit(0)


def load_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def save_json(path, value):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- platform


def tool_version(tool):
    path = shutil.which(tool)
    if not path:
        return None
    flag = "--version" if tool == "yt-dlp" else "-version"
    try:
        out = subprocess.run([path, flag], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "installed"
    first = (out.strip().splitlines() or ["installed"])[0]
    return first.replace("ffmpeg version ", "").replace("ffprobe version ", "").split(" Copyright")[0]


def missing_tools():
    return [t for t in ("ffmpeg", "ffprobe", "yt-dlp") if not shutil.which(t)]


def install_hint(missing):
    names = sorted({"ffmpeg" if t in ("ffmpeg", "ffprobe") else t for t in missing})
    if IS_MAC:
        return "brew install " + " ".join(names)
    parts = []
    if "ffmpeg" in names:
        parts.append("sudo apt install ffmpeg (or your package manager)")
    if "yt-dlp" in names:
        parts.append("pipx install yt-dlp")
    return " and ".join(parts)


def playback_ffmpeg():
    """Prefer the system build when it supports PulseAudio (bundled builds may not)."""
    system = "/usr/bin/ffmpeg"
    if not IS_MAC and os.path.isfile(system) and os.access(system, os.X_OK):
        return system
    return "ffmpeg"

def audio_output():
    """ffmpeg's output arguments for the speakers here, or None for silence."""
    if IS_MAC:
        return ["-f", "audiotoolbox", "-"]
    try:
        devices = subprocess.run([playback_ffmpeg(), "-hide_banner", "-devices"],
                                 capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    outputs = {line.split()[1] for line in devices.splitlines()
               if len(line.split()) > 1 and "E" in line.split()[0]}
    if "pulse" in outputs:  # PulseAudio, and PipeWire through its Pulse server
        return ["-f", "pulse", "Doomscroll"]
    if "alsa" in outputs:
        return ["-f", "alsa", "default"]
    return None


HWACCEL = ["-hwaccel", "videotoolbox"] if IS_MAC else []


# ---------------------------------------------------------------- terminal


def find_tty():
    """The tty of the nearest ancestor that has one (the claude process)."""
    pid = os.getppid()
    for _ in range(10):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out) < 2:
            return None
        ppid, tty = out[0], out[1]
        if tty not in ("??", "?", "-"):
            return tty if tty.startswith("/dev/") else "/dev/" + tty
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def term_size(tty):
    """rows, cols and the cell's pixel size, from the tty's window size."""
    if not tty or fcntl is None:
        return None
    try:
        fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            rows, cols, xpx, ypx = struct.unpack(
                "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        finally:
            os.close(fd)
    except OSError:
        return None
    if not rows or not cols:
        return None
    cell = (xpx / cols, ypx / rows) if xpx and ypx else None
    return {"rows": rows, "cols": cols, "cell": cell}


def kitty_background():
    """kitty's `background`, following `include` lines (themes live there)."""
    home = os.path.expanduser("~")
    base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "kitty")
    colour = None
    seen = set()

    def read(path, depth=0):
        nonlocal colour
        path = os.path.normpath(os.path.join(base, os.path.expanduser(path)))
        if depth > 4 or path in seen:
            return
        seen.add(path)
        try:
            with open(path) as f:
                for line in f:
                    words = line.split(None, 1)
                    if len(words) == 2 and words[0] == "background":
                        colour = normalise_hex(words[1]) or colour
                    elif len(words) == 2 and words[0] == "include":
                        read(words[1].strip(), depth + 1)
        except OSError:
            pass

    read("kitty.conf")
    return colour or "#000000"


def terminal_background():
    """The terminal's background colour, so the pane blends in rather than
    sitting in a black box: DOOMSCROLL_BACKGROUND if set, Ghostty's `background`
    or its theme's, or kitty's."""
    override = normalise_hex(os.environ.get("DOOMSCROLL_BACKGROUND", ""))
    if override:
        return override
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM") == "xterm-kitty":
        return kitty_background()
    if os.environ.get("TERM_PROGRAM", "").lower() != "ghostty":
        return None
    home = os.path.expanduser("~")
    configs = [os.path.join(home, ".config/ghostty/config"),
               os.path.join(home, "Library/Application Support/com.mitchellh.ghostty/config")]
    settings = {}
    for path in configs:
        try:
            with open(path) as f:
                for line in f:
                    key, _, value = line.partition("=")
                    if _ and not key.strip().startswith("#"):
                        settings[key.strip()] = value.strip().strip('"')
        except OSError:
            pass
    if settings.get("background"):
        return normalise_hex(settings["background"])
    theme = settings.get("theme")
    if not theme:
        return "#282c34"  # Ghostty's own default
    if "," in theme or ":" in theme:  # light:X,dark:Y follows the system appearance
        parts = dict(p.split(":", 1) for p in theme.split(",") if ":" in p)
        try:
            dark = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                  capture_output=True, text=True, timeout=2).stdout.strip() == "Dark"
        except (OSError, subprocess.SubprocessError):
            dark = True
        theme = parts.get("dark" if dark else "light", theme)
    for folder in (os.path.join(home, ".config/ghostty/themes"),
                   "/Applications/Ghostty.app/Contents/Resources/ghostty/themes"):
        try:
            with open(os.path.join(folder, theme)) as f:
                for line in f:
                    key, _, value = line.partition("=")
                    if key.strip() == "background":
                        return normalise_hex(value.strip())
        except OSError:
            pass
    return None


def normalise_hex(value):
    value = (value.split("#", 1)[-1].split() or [""])[0]
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value.lower()
    return None


def rgb_bytes(hex_colour):
    value = (hex_colour or "#000000").lstrip("#")
    return bytes(int(value[i:i + 2], 16) for i in (0, 2, 4))


# -------------------------------------------------------------------- feed


def slim(entry, handle):
    uploader = entry.get("uploader") or handle
    track = (entry.get("track") or "").strip()
    artists = entry.get("artists") or []
    music = f"{track} - {artists[0]}" if track and artists else track
    covers = {t.get("id"): t.get("url") for t in entry.get("thumbnails") or [] if t.get("url")}
    return {
        "id": str(entry["id"]),
        "cover": covers.get("cover") or covers.get("originCover"),
        "url": f"https://www.tiktok.com/@{uploader}/video/{entry['id']}",
        "author": uploader,
        "name": entry.get("channel") or uploader,
        "desc": " ".join((entry.get("description") or entry.get("title") or "").split()),
        "likes": entry.get("like_count"),
        "comments": entry.get("comment_count"),
        "shares": entry.get("repost_count"),
        "saves": entry.get("save_count"),
        "views": entry.get("view_count"),
        "music": music or "original sound",
        "duration": entry.get("duration"),
    }


def fetch_creator(handle):
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-J", "--no-warnings",
         "--playlist-end", str(PER_CREATOR), f"https://www.tiktok.com/@{handle}"],
        capture_output=True, text=True, timeout=120,
    )
    if not result.stdout.strip():
        raise RuntimeError((result.stderr.strip().splitlines() or ["no output"])[-1])
    data = json.loads(result.stdout)
    items = []
    for entry in data.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        if (entry.get("duration") or 0) > MAX_DURATION:
            continue
        items.append(slim(entry, handle))
    return items


class Feed:
    """A For You feed interleaved from a set of creators' recent posts."""

    def __init__(self, cache, creators, on_change):
        self.cache = cache
        self.on_change = on_change
        self.lock = threading.RLock()
        self.creators = []
        self.pool = {}
        self.items = []
        self.loading = set()
        self.failed = {}
        self.index_path = os.path.join(cache, "index.json")
        self.seen_path = os.path.join(cache, "seen.json")
        self.index = load_json(self.index_path, {})
        self.seen = list(load_json(self.seen_path, []))
        self.seen_set = set(self.seen)
        self.initial = creators
        self.mixed = []  # this session's sample of the For You mix

    def start(self, mix=True):
        if mix:
            pool = {h for handles in POOL.values() for h in handles}
            now = time.time()
            cached = [h for h, entry in self.index.items()
                      if h in pool and now - entry.get("at", 0) < CACHED_MAX_AGE and entry.get("items")]
            sampled = sample_pool()
            # Cached ones cost nothing to show; the sample is what gets fetched.
            self.mixed = list(dict.fromkeys(sampled + cached))
            self.fetchable = set(sampled)
        else:
            self.fetchable = set()
        self.set_creators(self.initial)

    def set_extras(self, extras):
        """The person's own creators, always in the feed beside the mix."""
        self.initial = [c for c in extras if c]
        self.set_creators(self.initial)

    def set_creators(self, creators):
        with self.lock:
            extras = [c.strip().lstrip("@").lower() for c in creators if c.strip()]
            self.fetchable = getattr(self, "fetchable", set()) | set(extras)
            wanted = list(dict.fromkeys(extras + self.mixed))
            added = [c for c in wanted if c not in self.creators]
            self.creators = wanted
            for gone in [c for c in self.pool if c not in wanted]:
                del self.pool[gone]
        if added:
            threading.Thread(target=self._load, args=(added,), daemon=True).start()

    def _load(self, handles):
        now = time.time()
        stale = []
        for handle in handles:
            cached = self.index.get(handle)
            if cached and cached.get("items"):
                self._add(handle, cached["items"])  # show it now, refresh below if old
                if now - cached.get("at", 0) < INDEX_TTL:
                    continue
            if handle in self.fetchable:
                stale.append(handle)
        if self.items or any(self.pool.values()):
            self.on_change()
        if not stale:
            return
        with self.lock:
            self.loading.update(stale)
        self.on_change()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_creator, h): h for h in stale}
            for future in as_completed(futures):
                handle = futures[future]
                try:
                    items = future.result()
                    self.index[handle] = {"at": time.time(), "items": items}
                    save_json(self.index_path, self.index)
                    self._add(handle, items)
                except Exception as error:  # noqa: BLE001 - one creator failing is fine
                    cached = self.index.get(handle)
                    if cached and cached.get("items"):
                        self._add(handle, cached["items"])
                    else:
                        self.failed[handle] = str(error)[:200]
                        emit(f"E could not list @{handle}: {str(error)[:160]}")
                with self.lock:
                    self.loading.discard(handle)
                self.on_change()

    def _add(self, handle, items):
        with self.lock:
            if handle not in self.creators:
                return
            queued = {item["id"] for item in self.items}
            fresh = [i for i in items if i["id"] not in queued]
            unseen = [i for i in fresh if i["id"] not in self.seen_set]
            seen = [i for i in fresh if i["id"] in self.seen_set]
            random.shuffle(unseen)
            random.shuffle(seen)
            self.pool[handle] = unseen + seen

    def ensure(self, count, need=None):
        """Grow the feed to at least `count` items, interleaving creators.

        While creators are still loading, it stops short (at `need`) rather
        than queue one creator back to back before the others arrive."""
        need = count if need is None else need
        with self.lock:
            while len(self.items) < count:
                heads = [(c, q[0]) for c, q in self.pool.items() if q]
                if not heads:
                    return
                last = self.items[-1]["author"].lower() if self.items else None
                unseen = [h for h in heads if h[1]["id"] not in self.seen_set]
                choices = unseen or heads
                varied = [h for h in choices if h[0] != last]
                is_thin = not varied or len(heads) < 6  # few creators in yet
                if is_thin and self.loading and len(self.items) >= need:
                    return
                varied = varied or choices
                handle, _ = random.choice(varied)
                self.items.append(self.pool[handle].pop(0))

    def get(self, index):
        self.ensure(index + 1)
        with self.lock:
            return self.items[index] if 0 <= index < len(self.items) else None

    def drop(self, index):
        with self.lock:
            if 0 <= index < len(self.items):
                self.items.pop(index)

    def mark_seen(self, item_id):
        with self.lock:
            if item_id in self.seen_set:
                return
            self.seen.append(item_id)
            self.seen_set.add(item_id)
            self.seen = self.seen[-3000:]
            save_json(self.seen_path, self.seen)

    def is_loading(self):
        with self.lock:
            return bool(self.loading)


# --------------------------------------------------------------- downloads


class Downloader:
    """Downloads the current video first, then the ones after it."""

    def __init__(self, folder, on_done):
        self.folder = folder
        self.on_done = on_done
        self.cv = threading.Condition()
        self.wanted = []
        self.in_use = set()  # the file the player has open, kept by _trim
        self.status = {}
        self.meta = {}
        for _ in range(3):
            threading.Thread(target=self._work, daemon=True).start()

    def path(self, item):
        return os.path.join(self.folder, item["id"] + ".mp4")

    def ready(self, item):
        return (item is not None and self.status.get(item["id"]) == "done"
                and os.path.exists(self.path(item)))

    def failed(self, item):
        return item is not None and self.status.get(item["id"]) == "failed"

    def want(self, items):
        with self.cv:
            self.wanted = [i for i in items if i]
            for item in self.wanted:
                path = self.path(item)
                exists = os.path.exists(path)
                if exists:
                    try:
                        os.utime(path)  # in use again: the trim keeps fresh files
                    except OSError:
                        pass
                if self.status.get(item["id"]) == "done" and not exists:
                    del self.status[item["id"]]  # gone from the cache: fetch it again
                if item["id"] not in self.status and exists:
                    probe = self._probe(path)
                    if probe:
                        self.meta[item["id"]] = probe
                        self.status[item["id"]] = "done"
            self.cv.notify_all()

    def _next_job(self):
        for item in self.wanted:
            if item["id"] not in self.status:
                self.status[item["id"]] = "downloading"
                return item
        return None

    def _work(self):
        while True:
            with self.cv:
                job = self._next_job()
                while job is None:
                    self.cv.wait()
                    job = self._next_job()
            ok = self._download(job)
            with self.cv:
                self.status[job["id"]] = "done" if ok else "failed"
            self._trim()
            self.on_done(job, ok)

    def _download(self, item):
        target = self.path(item)
        partial = os.path.join(self.folder, item["id"] + ".dl.%(ext)s")
        try:
            result = subprocess.run(
                ["yt-dlp", "-q", "--no-warnings", "--no-progress", "--no-playlist", "--no-mtime",
                 "-S", "res:1280", "-f", "b", "-o", partial, item["url"]],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode:
                emit(f"E video {item['id']}: {(result.stderr or result.stdout or 'yt-dlp failed').strip()[-300:]}")
                return False
        except (OSError, subprocess.SubprocessError) as error:
            emit(f"E video {item['id']}: {error}")
            return False
        got = [n for n in os.listdir(self.folder)
               if n.startswith(item["id"] + ".dl.") and not n.endswith(".part")]
        if not got:
            return False
        os.replace(os.path.join(self.folder, got[0]), target)
        probe = self._probe(target)
        if not probe:
            try:
                os.remove(target)
            except OSError:
                pass
            return False
        self.meta[item["id"]] = probe
        return True

    @staticmethod
    def _probe(path):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type:format=duration", "-of", "json", path],
                capture_output=True, text=True, timeout=20,
            ).stdout
            data = json.loads(out or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        kinds = {s.get("codec_type") for s in data.get("streams", [])}
        if "video" not in kinds:
            return None
        duration = float((data.get("format") or {}).get("duration") or 0)
        return {"audio": "audio" in kinds, "duration": duration}

    def _trim(self):
        try:
            files = [os.path.join(self.folder, n) for n in os.listdir(self.folder)
                     if n.endswith(".mp4") and ".dl." not in n]
        except OSError:
            return
        if len(files) <= KEEP_VIDEOS:
            return
        keep = {self.path(i) for i in self.wanted} | self.in_use
        fresh = time.time() - 15 * 60  # never anything fetched in the last quarter hour
        files.sort(key=lambda p: os.path.getmtime(p))
        for path in files[: len(files) - KEEP_VIDEOS]:
            if path in keep or os.path.getmtime(path) > fresh:
                continue
            try:
                os.remove(path)
                self.status.pop(os.path.basename(path)[:-4], None)
            except OSError:
                pass


# ------------------------------------------------------------------ player


def read_exact(stream, view, size):
    got = 0
    while got < size:
        n = stream.readinto(view[got:size])
        if not n:
            return False
        got += n
    return True


class Player:
    """One ffmpeg per playback: frames to shared memory, sound to the speakers."""

    def __init__(self, frames_dir, on_end, background="#000000"):
        self.background = background
        self.audio_out = audio_output()
        self.frames_dir = frames_dir
        self.on_end = on_end
        self.proc = None
        self.epoch = 0
        self.gen = 0
        self.pos = 0.0
        self.last = None
        self.lock = threading.RLock()
        self.shm_names = deque()
        self.use_shm = _posixshmem is not None and os.environ.get("DOOMSCROLL_TRANSPORT") != "file"
        self.prefix = f"/dsc{os.getpid() % 100000}-"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, path, pos, duration, size, audio):
        with self.lock:
            self.stop()
            self.epoch += 1
            epoch = self.epoch
        width, height = size
        vf = f"fps={FPS}," + vf_for(width, height, self.background)
        audio = audio and self.audio_out is not None
        attempts = ([(True, audio)] if HWACCEL else []) + [(False, audio)] + ([(False, False)] if audio else [])
        for hwaccel, with_audio in attempts:
            argv = [playback_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error"]
            if hwaccel:
                argv += HWACCEL
            argv += ["-ss", f"{max(0.0, pos):.3f}", "-re", "-i", path,
                     "-map", "0:v:0", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
            if with_audio:
                argv += ["-map", "0:a:0"] + self.audio_out
            with self.lock:
                if epoch != self.epoch:
                    return
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        stdin=subprocess.DEVNULL, bufsize=0)
                self.proc = proc
            threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
            if self._read(proc, epoch, width, height, pos, duration) or epoch != self.epoch:
                return
        if epoch == self.epoch:
            emit(f"E ffmpeg could not play {os.path.basename(path)}")
            threading.Thread(target=self.on_end, args=(epoch, False), daemon=True).start()

    def _drain(self, proc):
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    emit("E ffmpeg: " + text[:200])
        except (OSError, ValueError):
            pass

    def _read(self, proc, epoch, width, height, pos0, duration):
        """Reads the first frame here; the rest on a thread. Returns frames read."""
        size = width * height * 3
        buf = bytearray(size)
        view = memoryview(buf)
        if not read_exact(proc.stdout, view, size):
            proc.wait()
            return 0
        self._publish(bytes(buf), width, height, pos0, duration, epoch)

        def rest():
            frames = 1
            while read_exact(proc.stdout, view, size):
                if epoch != self.epoch:
                    break
                frames += 1
                self._publish(bytes(buf), width, height, pos0 + frames / FPS, duration, epoch)
            proc.wait()
            if epoch == self.epoch:
                self.on_end(epoch, True)

        threading.Thread(target=rest, daemon=True).start()
        return 1

    def _publish(self, frame, width, height, pos, duration, epoch):
        if epoch != self.epoch:
            return
        self.gen += 1
        self.pos = pos
        self.last = (frame, width, height, self.gen)
        if self.use_shm:
            name = f"{self.prefix}{self.gen}"
            try:
                fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                try:
                    os.ftruncate(fd, len(frame))
                    with mmap.mmap(fd, len(frame)) as m:
                        m[: len(frame)] = frame
                finally:
                    os.close(fd)
                self.shm_names.append(name)
                while len(self.shm_names) > SHM_KEEP:
                    stale = self.shm_names.popleft()
                    try:
                        _posixshmem.shm_unlink(stale)
                    except OSError:
                        pass
                emit(f"F shm {name} {self.gen} {width} {height} {pos:.2f} {duration:.2f}")
                return
            except OSError as error:
                emit(f"E shared memory failed ({error}); using files")
                self.use_shm = False
        path = os.path.join(self.frames_dir, f"{self.gen % FILE_RING}.rgb")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(frame)
        os.replace(tmp, path)
        emit(f"F file {path} {self.gen} {width} {height} {pos:.2f} {duration:.2f}")

    def show(self, frame, size, duration, epoch=None):
        """Publishes a still (a preroll, a slide step) as the current frame."""
        width, height = size
        self._publish(frame, width, height, 0.0, duration, self.epoch if epoch is None else epoch)

    def poster(self):
        """The last frame as a plain file a redraw can read again."""
        if not self.last:
            return None
        frame, width, height, gen = self.last
        path = os.path.join(self.frames_dir, "poster.rgb")
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(frame)
            os.replace(tmp, path)
        except OSError:
            return None
        return {"file": path, "width": width, "height": height, "generation": gen}

    def stop(self):
        with self.lock:
            self.epoch += 1
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            # SIGKILL: ffmpeg ignores SIGTERM while blocked writing a frame nobody
            # reads any more, which cost every swipe a second and a half.
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def cleanup(self):
        self.stop()
        while self.shm_names and _posixshmem:
            try:
                _posixshmem.shm_unlink(self.shm_names.popleft())
            except OSError:
                pass


# ---------------------------------------------------------------- prerolls


def vf_for(width, height, background="#000000"):
    colour = "0x" + background.lstrip("#")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={colour}")


class Prerolls:
    """First frames of the videos around the current one, decoded ahead at the
    pane's size, so a swipe can slide to the next video at once. A video not
    downloaded yet starts from its cover image instead."""

    def __init__(self, folder, downloads, background="#000000"):
        self.background = background
        self.folder = folder
        self.downloads = downloads
        self.frames = OrderedDict()
        self.lock = threading.Lock()
        self.cv = threading.Condition()
        self.queue = []
        threading.Thread(target=self._work, daemon=True).start()

    def cached(self, item, size):
        key = (item["id"], size, self.downloads.ready(item))
        with self.lock:
            return self.frames.get(key)

    def get(self, item, size):
        """The first frame now: cached, else decoded here (tens of ms)."""
        if item is None:
            return None
        return self.cached(item, size) or self._make(item, size)

    def warm(self, items, size):
        with self.cv:
            self.queue = [(i, size) for i in items if i]
            self.cv.notify()

    def _work(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                item, size = self.queue.pop(0)
            if not self.cached(item, size):
                self._make(item, size)

    def _cover(self, item):
        url = item.get("cover")
        if not url:
            return None
        path = os.path.join(self.folder, item["id"] + ".jpg")
        if os.path.exists(path):
            return path
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/"})
            with urllib.request.urlopen(request, timeout=6) as response:
                data = response.read()
            with open(path, "wb") as f:
                f.write(data)
            self._trim()
            return path
        except (OSError, ValueError):
            return None

    def _trim(self):
        try:
            covers = sorted((os.path.join(self.folder, n) for n in os.listdir(self.folder)),
                            key=os.path.getmtime)
        except OSError:
            return
        for path in covers[:-300]:
            try:
                os.remove(path)
            except OSError:
                pass

    def _make(self, item, size):
        is_video = self.downloads.ready(item)
        source = self.downloads.path(item) if is_video else self._cover(item)
        if not source:
            return None
        width, height = size
        try:
            frame = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", source, "-frames:v", "1",
                 "-vf", vf_for(width, height, self.background), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                capture_output=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if len(frame) != width * height * 3:
            return None
        with self.lock:
            self.frames[(item["id"], size, is_video)] = frame
            while len(self.frames) > PREROLL_KEEP:
                self.frames.popitem(last=False)
        return frame


# ------------------------------------------------------------------ daemon


class Daemon:
    def __init__(self, args):
        self.cache = os.path.expanduser(args.cache)
        os.makedirs(os.path.join(self.cache, "videos"), exist_ok=True)
        self.frames_dir = tempfile.mkdtemp(prefix="doomscroll-")
        self.sock = args.sock
        self.lock = threading.RLock()
        self.index = -1
        self.want_play = False
        self.muted = bool(args.muted) or os.environ.get("DOOMSCROLL_MUTED") == "1"
        self.watched = set()
        self.message = None
        self.box = (36, 32)
        self.tty = find_tty()
        self.term = term_size(self.tty)
        self.loading_item = None
        self.missing = missing_tools()
        self.background = terminal_background() or "#000000"
        self.player = Player(self.frames_dir, self._ended, self.background)
        self.downloads = Downloader(os.path.join(self.cache, "videos"), self._downloaded)
        covers = os.path.join(self.cache, "covers")
        os.makedirs(covers, exist_ok=True)
        self.prerolls = Prerolls(covers, self.downloads, self.background)
        self.is_sliding = False
        self.personal = getattr(args, 'personal', False)
        if self.personal:
            from personal import PersonalFeed
            self.feed = PersonalFeed(self.cache, self._feed_changed)
        else:
            self.feed = Feed(self.cache, [c for c in args.creators.split(",") if c], self._feed_changed)
        self.mix = not args.no_mix
        self.server = None

    # -- state ------------------------------------------------------------

    def cell(self):
        if self.term and self.term.get("cell"):
            return self.term["cell"]
        return DEFAULT_CELL

    def frame_size(self):
        cols, rows = self.box
        cw, ch = self.cell()
        box_w, box_h = cols * cw, rows * ch
        height = min(MAX_FRAME_H, int(box_h))
        width = int(round(height * box_w / box_h))
        return max(16, width - width % 2), max(16, height - height % 2)

    def state(self):
        with self.lock:
            item = self.feed.get(self.index) if self.index >= 0 else None
            message = self.message
            if self.personal and self.feed.failed:
                message = self.feed.failed.get('personal')
            if self.missing:
                status = "empty"
                message = (f"Doomscroll needs {', '.join(self.missing)}. "
                           f"Install with: {install_hint(self.missing)}, then /doomscroll.")
            elif item is not None and self.want_play:
                ready = self.downloads.ready(item) or self.player.running() or self.is_sliding
                status = "playing" if ready else "loading"
            elif item is not None:
                status = "paused"
            elif self.feed.is_loading() or not self.feed.creators:
                status = "warming" if self.feed.creators else "empty"
                if not self.feed.creators and not message:
                    message = "No creators to show. /doomscroll add @someone, or turn the For You mix back on."
            else:
                status = "empty"
            upcoming = self.feed.items[max(0, self.index):self.index + AHEAD + 1]  # no growing here
            ahead = sum(1 for i in upcoming if self.downloads.ready(i))
            meta = self.downloads.meta.get(item["id"]) if item else None
            if item and meta and not item.get("duration"):
                item = {**item, "duration": round(meta["duration"])}
            cw, ch = self.cell()
            return {
                "status": status,
                "feed_mode": "personal" if self.personal else "guest",
                "index": self.index,
                "total": len(self.feed.items),
                "item": item,
                "muted": self.muted,
                "watched": len(self.watched),
                "message": message,
                "poster": self.player.poster(),
                "ahead": ahead,
                "cell": {"cw": cw, "ch": ch},
                "bg": self.background,
                "term": {"rows": self.term["rows"], "cols": self.term["cols"]} if self.term else None,
            }

    def publish(self):
        emit("S " + json.dumps(self.state()))

    # -- events from the workers -----------------------------------------

    def _feed_changed(self):
        with self.lock:
            if self.index < 0 and self.feed.get(0):
                self.index = 0
                self._prefetch()
                if self.want_play:
                    self._play_current()
            elif self.index >= 0:
                self._prefetch()  # a creator arrived: queue and fetch further ahead
            if self.index < 0 and not self.feed.is_loading() and self.feed.failed:
                self.message = (self.feed.failed.get('personal') if self.personal else
                                "Couldn't reach TikTok. Check your connection, update yt-dlp, or /doomscroll add @someone.")
        self.publish()

    def _downloaded(self, item, ok):
        with self.lock:
            current = self.feed.get(self.index) if self.index >= 0 else None
            if current and current["id"] == item["id"]:
                if ok and self.want_play and not self.player.running() and not self.is_sliding:
                    self._play_current()
                elif not ok:
                    self.feed.drop(self.index)
                    self._prefetch()
                    if self.want_play:
                        self._play_current()
        self.publish()

    def _ended(self, epoch, ok):
        with self.lock:
            if epoch != self.player.epoch:
                return
            if not ok:
                self.feed.drop(self.index)
                self.player.pos = 0.0
                self._prefetch()
                if self.want_play:
                    self._play_current()
            else:
                self.want_play = False
                self.player.stop()
                self.player.pos = 0.0
        self.publish()

    # -- control -------------------------------------------------------------

    def _prefetch(self):
        if self.index < 0:
            return
        self.feed.ensure(self.index + AHEAD + 1, need=self.index + 2)
        order = [self.feed.items[i] for i in range(self.index, min(len(self.feed.items), self.index + AHEAD + 1))]
        if self.index > 0:
            order.append(self.feed.get(self.index - 1))
        self.downloads.want(order)
        self.prerolls.warm(order[1:] + order[:1], self.frame_size())

    def _play_current(self, pos=None):
        item = self.feed.get(self.index)
        if item is None:
            return
        if not self.downloads.ready(item):
            self.player.stop()
            self._prefetch()
            return
        meta = self.downloads.meta.get(item["id"]) or {}
        start = self.player.pos if pos is None else pos
        duration = meta.get("duration") or item.get("duration") or 0
        if duration and start >= duration - 0.3:
            start = 0.0
        self.watched.add(item["id"])
        self.feed.mark_seen(item["id"])
        self.message = None
        size = self.frame_size()
        audio = not self.muted and meta.get("audio", True)
        self.player.stop()
        self.downloads.in_use = {self.downloads.path(item)}
        threading.Thread(
            target=self.player.start,
            args=(self.downloads.path(item), start, duration, size, audio),
            daemon=True,
        ).start()

    def _goto(self, index, direction):
        if index < 0:
            index = 0
        self.feed.ensure(index + 1)
        if index >= len(self.feed.items):
            index = max(0, len(self.feed.items) - 1)
        if index == self.index:
            return
        last = self.player.last
        self.player.stop()
        self.index = index
        self.player.pos = 0.0
        self._prefetch()
        size = self.frame_size()
        width, height = size
        item = self.feed.get(index)
        # Never fetch a cover here (this holds the lock): a cached first frame,
        # a quick decode of a downloaded video, or black while it loads.
        target = None
        if item is not None:
            target = self.prerolls.cached(item, size) or (
                self.prerolls.get(item, size) if self.downloads.ready(item) else None)
        target = target or rgb_bytes(self.background) * (width * height)
        if last is not None and (last[1], last[2]) == size:
            self.is_sliding = True
            epoch = self.player.epoch
            threading.Thread(target=self._slide, args=(last[0], target, size, direction, epoch),
                             daemon=True).start()
            return
        self.is_sliding = False
        self.player.show(target, size, self._duration())
        if self.want_play:
            self._play_current(0.0)

    def _duration(self):
        item = self.feed.get(self.index)
        meta = self.downloads.meta.get(item["id"]) if item else None
        return (meta or {}).get("duration") or (item or {}).get("duration") or 0

    def _slide(self, old, new, size, direction, epoch):
        """TikTok's swipe: the old video leaves upward as the next one rises
        (or the reverse going back), easing out over a fifth of a second."""
        width, height = size
        row = width * 3
        duration = self._duration()
        start = time.monotonic()
        for k in range(1, SLIDE_FRAMES + 1):
            if epoch != self.player.epoch:
                return
            eased = 1 - (1 - k / SLIDE_FRAMES) ** 3
            cut = int(round(height * eased))
            if direction > 0:
                frame = old[cut * row:] + new[: cut * row]
            else:
                frame = new[(height - cut) * row:] + old[: (height - cut) * row]
            self.player.show(frame, size, duration, epoch)
            wait = start + k * SLIDE_MS / 1000 / SLIDE_FRAMES - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        with self.lock:
            if epoch != self.player.epoch:
                return
            self.is_sliding = False
            if self.want_play:
                self._play_current(0.0)
        self.publish()

    def command(self, body):
        op = body.get("op")
        with self.lock:
            if op == "play":
                self.want_play = True
                if self.index < 0 and self.feed.get(0):
                    self.index = 0
                if not self.player.running() and not self.is_sliding:
                    self._play_current()
            elif op == "pause":
                self.want_play = False
                self.is_sliding = False
                self.player.stop()
            elif op == "next":
                self.want_play = True
                self._goto(self.index + 1, 1)
            elif op == "prev":
                self.want_play = True
                self._goto(self.index - 1, -1)
            elif op == "mute":
                self.muted = bool(body.get("muted", not self.muted))
                if self.player.running():
                    self._play_current()
            elif op == "size":
                box = (int(body.get("cols", 0)), int(body.get("rows", 0)))
                self.term = term_size(self.tty) or self.term
                if box[0] > 0 and box[1] > 0 and box != self.box:
                    before = self.frame_size()
                    self.box = box
                    if self.player.running() and self.frame_size() != before:
                        self._play_current()
            elif op == "creators":
                self.feed.set_extras(body.get("creators") or [])
            elif op == "open":
                item = self.feed.get(self.index)
                opener = "open" if IS_MAC else "xdg-open"
                if item and shutil.which(opener):
                    subprocess.Popen([opener, item["url"]], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            elif op == "recheck":
                was_missing = self.missing
                self.missing = missing_tools()
                if was_missing and not self.missing:
                    self.player.audio_out = audio_output()
                    self.feed.start(self.mix)
            elif op == "doctor":
                return self.doctor()
            elif op not in (None, "state"):
                raise ValueError(f"unknown op {op!r}")
        state = self.state()
        emit("S " + json.dumps(state))
        return state

    def doctor(self):
        """What a bug report needs: platform, tools, audio, terminal, feed."""
        with self.lock:
            return {
                "doctor": {
                    "platform": sys.platform,
                    "python": sys.version.split()[0],
                    "tools": {t: tool_version(t) for t in ("ffmpeg", "ffprobe", "yt-dlp")},
                    "audio": " ".join(self.player.audio_out or []) or "none (muted)",
                    "hwaccel": " ".join(HWACCEL) or "software decode",
                    "frames": "shared memory" if self.player.use_shm else "files",
                    "terminal": {"tty": self.tty, "size": self.term, "background": self.background},
                    "box": list(self.box),
                    "frame": list(self.frame_size()),
                    "cache": self.cache,
                    "creators": len(self.feed.creators),
                    "yours": self.feed.initial,
                    "loading": sorted(self.feed.loading),
                    "failed": self.feed.failed,
                    "feed": len(self.feed.items),
                    "queue": [i["author"] for i in self.feed.items[max(0, self.index):self.index + 8]],
                    "downloaded": sum(1 for v in self.downloads.status.values() if v == "done"),
                    "errors": list(RECENT_ERRORS),
                }
            }

    # -- lifecycle -----------------------------------------------------------

    def serve(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's naming
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    payload, code = daemon.command(body), 200
                except Exception as error:  # noqa: BLE001 - report, keep serving
                    payload, code = {"error": str(error)}, 400
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def address_string(self):
                return "unix"

            def log_message(self, *_):
                pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        try:
            os.unlink(self.sock)
        except OSError:
            pass
        self.server = Server(self.sock, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def shutdown(self):
        if self.personal:
            self.feed.close()
        self.player.cleanup()
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        shutil.rmtree(self.frames_dir, ignore_errors=True)


def watch_parent():
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            die()


def main():
    global DAEMON
    parser = argparse.ArgumentParser()
    parser.add_argument("--sock", required=True)
    parser.add_argument("--cache", default=os.path.join(os.environ.get("XDG_CACHE_HOME") or "~/.cache", "doomscroll"))
    parser.add_argument("--creators", default="", help="your own handles, always in the feed")
    parser.add_argument("--no-mix", action="store_true", help="only your creators, no For You mix")
    parser.add_argument("--muted", action="store_true")
    parser.add_argument("--personal", action="store_true", help="opt-in logged-in browser For You feed; guest is default")
    args = parser.parse_args()

    if IS_WINDOWS:
        # No Unix sockets, POSIX shared memory or kitty-graphics terminal here.
        emit("S " + json.dumps({
            "status": "empty", "index": -1, "total": 0, "item": None, "muted": True,
            "watched": 0, "poster": None, "ahead": 0, "cell": None, "term": None,
            "message": "Doomscroll runs on macOS and Linux, in Ghostty or kitty."}))
        while True:
            time.sleep(3600)

    signal.signal(signal.SIGTERM, die)
    signal.signal(signal.SIGINT, die)
    signal.signal(signal.SIGHUP, die)
    DAEMON = Daemon(args)
    DAEMON.serve()
    if DAEMON.missing:
        emit(f"E missing {', '.join(DAEMON.missing)}: {install_hint(DAEMON.missing)}")
    else:
        DAEMON.feed.start(DAEMON.mix)
    threading.Thread(target=watch_parent, daemon=True).start()
    emit(f"R {args.sock}")
    DAEMON.publish()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
