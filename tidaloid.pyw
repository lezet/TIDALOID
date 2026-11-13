"""TIDALOID - Windows downloader built on tidalapi (tkinter, no extra UI dependencies).

Sign-in:
  * Quick sign-in    - device link (approve in browser). TIDAL caps these sessions at 320 kbps AAC.
  * Lossless sign-in - PKCE browser login (paste the 'Oops' page URL back). Allows FLAC / Hi-Res.
Files: settings ~/.tidaloid.json, session ~/.tidaloid-session.json
"""
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------- output capture
LOG_Q: "queue.Queue[str]" = queue.Queue()


class _QueueWriter:
    encoding = "utf-8"

    def write(self, text):
        if text:
            LOG_Q.put(text)
        return len(text or "")

    def flush(self):
        pass

    def isatty(self):
        return False


sys.stdout = sys.stderr = _QueueWriter()

import requests                    # noqa: E402
import tidalapi                    # noqa: E402
from tidalapi.media import Quality  # noqa: E402

APP_TITLE = "TIDALOID"
__version__ = "1.0.0"


def app_dir() -> Path:
    """Folder holding the script, or the .exe when built with PyInstaller."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def app_icon_candidates():
    """A tidaloid.ico next to the app overrides the bundled icon."""
    bundled = Path(getattr(sys, "_MEIPASS", app_dir())) / "assets" / "tidaloid.ico"
    return [app_dir() / "tidaloid.ico", bundled]
HOME = Path.home()
SETTINGS_FILE = HOME / ".tidaloid.json"
SESSION_FILE = HOME / ".tidaloid-session.json"
# move files from the app's earlier name ("tidal-dl GUI") once
for _old, _new in ((HOME / ".tidal-dl-gui.json", SETTINGS_FILE), (HOME / ".tidalapi-session.json", SESSION_FILE)):
    if _old.exists() and not _new.exists():
        try:
            _old.rename(_new)
        except OSError:
            pass
PAGE_SIZE = 50
THUMB_SIZE = 44      # px, cover thumbnails in the result list
PREVIEW_SIZE = 200   # px, large cover in the preview pane
SEGMENT_WORKERS = 8  # parallel segment downloads per track
TRACK_WORKERS = 3     # tracks downloaded at the same time
SEARCH_LIMIT = 300  # TIDAL never returns more than 300 search hits

QUALITIES = {  # label -> tidalapi quality
    "Low (AAC 96 kbps)": Quality.low_96k,
    "High (AAC 320 kbps)": Quality.low_320k,
    "Lossless (FLAC 16-bit)": Quality.high_lossless,
    "Max (FLAC Hi-Res)": Quality.hi_res_lossless,
}
SEARCH_TYPES = {"Tracks": tidalapi.Track, "Albums": tidalapi.Album, "Artists": tidalapi.Artist,
                "Playlists": tidalapi.Playlist, "Videos": tidalapi.Video}
RESULT_KEY = {"Tracks": "tracks", "Albums": "albums", "Artists": "artists",
              "Playlists": "playlists", "Videos": "videos"}
URL_RE = re.compile(r"(track|album|playlist|artist|video|mix)/([0-9A-Za-z-]+)", re.I)

DEFAULT_SETTINGS = {
    "quality": "Lossless (FLAC 16-bit)",
    "folder": str(HOME / "Music" / "TIDALOID"),
    "lyrics": False,
    "covers": True,
    "skip_existing": True,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if s["quality"] not in QUALITIES:
        s["quality"] = DEFAULT_SETTINGS["quality"]
    return s


def save_settings(s):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Could not save settings: {e}")


# ---------------------------------------------------------------- helpers
def fmt_time(seconds):
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def quality_label(obj):
    tags = getattr(obj, "media_metadata_tags", None) or []
    if "HIRES_LOSSLESS" in tags:
        return "Hi-Res"
    q = getattr(obj, "audio_quality", "") or ""
    return {"LOW": "Low", "HIGH": "High", "LOSSLESS": "Lossless",
            "HI_RES": "Master", "HI_RES_LOSSLESS": "Hi-Res"}.get(q, q)


def artists_of(obj):
    arts = getattr(obj, "artists", None) or []
    names = [a.name for a in arts if getattr(a, "name", None)]
    if not names and getattr(obj, "artist", None) is not None:
        names = [obj.artist.name]
    return ", ".join(names)


def title_of(obj):
    name = getattr(obj, "name", "") or ""
    ver = getattr(obj, "version", None)
    return f"{name} ({ver})" if ver and ver.lower() not in name.lower() else name


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name, maxlen=120):
    name = _BAD.sub("_", str(name or "Unknown")).strip().rstrip(".")
    return (name or "Unknown")[:maxlen].strip()


def year_of(album):
    try:
        return album.year or ""
    except Exception:
        return ""


def plural(n, word):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n} {word}" + ("" if n == 1 else "s")


def image_urls(label, it):
    """(thumbnail URL, large preview URL) for a search result, or (None, None)."""
    try:
        if label == "Tracks":
            al = it.album
            return (al.image(80), al.image(320)) if al is not None and al.cover else (None, None)
        if label == "Albums":
            return (it.image(80), it.image(320)) if it.cover else (None, None)
        if label == "Artists":
            return (it.image(160), it.image(320)) if getattr(it, "picture", None) else (None, None)
        if label == "Playlists":
            return it.image(160), it.image(320)
        if label == "Videos":
            return it.image(160, 107), it.image(480, 320)
    except Exception:
        pass
    return None, None


def ffplay_path():
    ff = ffmpeg_path()
    if not ff:
        return None
    p = Path(ff).with_name("ffplay.exe")
    return str(p) if p.exists() else shutil.which("ffplay")


# SDL's default WASAPI output can play silently (and far too fast) with some pro-audio interfaces;
# WinMM goes to the Windows default playback device at the correct speed.
PLAYER_ENV = dict(os.environ, SDL_AUDIODRIVER="winmm")
PREVIEW_VOLUME = 80   # ffplay volume, 0-100


class Player:
    """Streams a TIDAL track into ffplay (no window). Supports seeking: playback is restarted at the
    new position, and already-downloaded DASH segments are kept so jumping back is instant."""

    def __init__(self, on_state):
        self.on_state = on_state      # on_state(playing: bool, text: str) - called from worker threads
        self.proc = None
        self.gen = 0
        self._lock = threading.Lock()
        self._session = None
        self._session_key = None
        self.http = requests.Session()
        self.src = None               # the loaded track: dict(urls, cache, segdur, tmp, name)
        self.duration = 0.0
        self._pos0 = 0.0              # position playback (re)started at
        self._t0 = None               # monotonic time audio started flowing

    def _preview_session(self, main: tidalapi.Session):
        """A separate session so preview quality never clashes with running downloads."""
        key = main.access_token
        if self._session is None or self._session_key != key:
            s = tidalapi.Session()
            s.load_oauth_session(main.token_type, main.access_token, main.refresh_token,
                                 is_pkce=main.is_pkce)
            s.audio_quality = Quality.low_320k
            self._session, self._session_key = s, key
        return self._session

    # ---- public API (UI thread)
    def active(self):
        return self.src is not None

    def position(self):
        with self._lock:
            if self.src is None:
                return 0.0
            if self._t0 is None:
                return self._pos0
            return min(self._pos0 + time.monotonic() - self._t0, self.duration)

    def stop(self):
        with self._lock:
            self.gen += 1
            p, self.proc = self.proc, None
            src, self.src = self.src, None
        self._kill(p)
        if src and src.get("tmp"):
            threading.Thread(target=self._remove, args=(src["tmp"],), daemon=True).start()

    def play(self, main_session, resolve, label):
        """resolve(session) -> tidalapi.Track to play (runs in a worker thread)."""
        self.stop()
        with self._lock:
            gen = self.gen
        threading.Thread(target=self._load, args=(main_session, resolve, label, gen), daemon=True).start()

    def seek(self, pos):
        """Jump to pos seconds in the loaded track."""
        with self._lock:
            if self.src is None:
                return
            pos = max(0.0, min(float(pos), max(self.duration - 1.0, 0.0)))
            self.gen += 1
            gen = self.gen
            p, self.proc = self.proc, None
            self._pos0, self._t0 = pos, None
            src = self.src
        self._kill(p)
        threading.Thread(target=self._play_from, args=(src, pos, gen), daemon=True).start()

    # ---- internals (worker threads)
    @staticmethod
    def _kill(p):
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    @staticmethod
    def _remove(path):
        for _ in range(10):  # ffplay may still hold the file for a moment
            try:
                path.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.3)

    def _current(self, gen):
        with self._lock:
            return gen == self.gen

    def _load(self, main_session, resolve, label, gen):
        if not ffplay_path():
            self.on_state(False, "Preview needs FFmpeg (ffplay) - not found.")
            return
        try:
            self.on_state(True, f"Loading {label}...")
            sess = self._preview_session(main_session)
            track = resolve(sess)
            if track is None:
                self.on_state(False, "Nothing to play here.")
                return
            manifest = track.get_stream().get_stream_manifest()
            urls = manifest.get_urls()
            src = {"urls": urls, "cache": {}, "segdur": 0.0, "tmp": None,
                   "name": f"{artists_of(track)} - {title_of(track)}"}
            duration = float(track.duration or 0)
            di = getattr(manifest, "dash_info", None)
            if len(urls) > 1 and di is not None and di.timescale:
                src["segdur"] = di.chunk_size / di.timescale
                duration = di.duration.total_seconds() or duration
            elif len(urls) == 1:
                # single progressive file: its index may sit at the end, so fetch it before playing
                import tempfile
                tmp = Path(tempfile.gettempdir()) / f"tidal-preview-{os.getpid()}.m4a"
                tmp.write_bytes(self.http.get(urls[0], timeout=60).content)
                src["tmp"] = tmp
            with self._lock:
                if gen != self.gen:
                    if src["tmp"]:
                        threading.Thread(target=self._remove, args=(src["tmp"],), daemon=True).start()
                    return
                self.src, self.duration = src, duration
                self._pos0, self._t0 = 0.0, None
            self._play_from(src, 0.0, gen)
        except Exception as e:
            if self._current(gen):
                with self._lock:
                    self.src = None
                self.on_state(False, f"Preview failed: {str(e)[:100]}")

    def _segment(self, src, i):
        data = src["cache"].get(i)
        if data is None:
            data = self.http.get(src["urls"][i], timeout=30).content
            src["cache"][i] = data
        return data

    def _play_from(self, src, pos, gen):
        try:
            cmd = [ffplay_path(), "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(PREVIEW_VOLUME)]
            if src["tmp"]:
                cmd += ["-ss", f"{pos:.2f}", "-i", str(src["tmp"])]
                order = []
            else:
                # first url = init segment; segment k (k >= 1) starts at (k-1) * segdur.
                # The fragments keep their real timestamps, so atrim can cut to the exact second.
                k = 1
                if pos > 0 and src["segdur"]:
                    k = 1 + int(max(pos - 0.1, 0) // src["segdur"])
                    cmd += ["-af", f"atrim=start={pos:.3f}"]
                k = min(k, len(src["urls"]) - 1)
                cmd += ["-i", "pipe:0"]
                order = [0] + list(range(k, len(src["urls"])))
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=PLAYER_ENV, creationflags=0x08000000)
            with self._lock:
                if gen != self.gen:
                    self._kill(proc)
                    return
                self.proc = proc
                if src["tmp"]:
                    self._t0 = time.monotonic()
            self.on_state(True, f"♫ Playing: {src['name']}")
            if order:
                ex = ThreadPoolExecutor(4)
                futs = {}
                try:
                    for j, i in enumerate(order):
                        for ahead in order[j:j + 8]:      # keep a few segments downloading ahead
                            if ahead not in futs:
                                futs[ahead] = ex.submit(self._segment, src, ahead)
                        data = futs.pop(i).result()
                        if not self._current(gen):
                            return
                        if j == 1:   # audio starts as soon as the first media segment flows in
                            with self._lock:
                                if gen == self.gen:
                                    self._t0 = time.monotonic()
                        proc.stdin.write(data)   # blocks at real-time pace once ffplay's buffer is full
                except (BrokenPipeError, OSError):
                    return
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
            with self._lock:
                finished = gen == self.gen
            if finished:          # ffplay exited on its own
                early = self.position() < self.duration - 5
                self.stop()
                self.on_state(False, "Playback stopped early (audio device busy?) - press Play again."
                              if early else "")
        except Exception as e:
            if self._current(gen):
                self.stop()
                self.on_state(False, f"Preview failed: {str(e)[:100]}")


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget installs a link here; it may not be on PATH for already-running processes
    local = os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local"))
    for cand in (Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",):
        if cand.exists():
            return str(cand)
    pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
    hits = sorted(pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkgs.exists() else []
    return str(hits[-1]) if hits else None


# ---------------------------------------------------------------- downloader
class Downloader:
    def __init__(self, session: tidalapi.Session, settings: dict, progress_cb, cancel: threading.Event):
        self.session = session
        self.s = settings
        self.progress = progress_cb      # progress(fraction 0..1 or None, label)
        self.cancel = cancel
        self.ok = self.failed = self.skipped = 0
        self._cover_cache = {}
        self._lock = threading.Lock()
        self.single = True
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=SEGMENT_WORKERS * TRACK_WORKERS + 4)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)
        self.seg_pool = ThreadPoolExecutor(SEGMENT_WORKERS * TRACK_WORKERS)

    def count(self, field):
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def close(self):
        self.seg_pool.shutdown(wait=False, cancel_futures=True)

    # ---- entry point
    def run(self, url):
        m = URL_RE.search(url)
        if not m:
            print(f"Not a TIDAL link: {url}")
            self.count("failed")
            return
        kind, ident = m.group(1).lower(), m.group(2)
        if kind == "track":
            t = self.session.track(ident)
            self.download_tracks([t], None)
        elif kind == "album":
            self.download_album(self.session.album(ident))
        elif kind == "playlist":
            pl = self.session.playlist(ident)
            print(f"Playlist: {pl.name} ({pl.num_tracks} tracks)")
            tracks = self._all(lambda lim, off: pl.tracks(limit=lim, offset=off))
            folder = Path(self.s["folder"]) / "Playlists" / safe(pl.name)
            self.download_tracks(tracks, folder, numbered=True)
        elif kind == "mix":
            mx = self.session.mix(ident)
            items = [i for i in mx.items() if isinstance(i, tidalapi.Track)]
            print(f"Mix: {mx.title} ({len(items)} tracks)")
            self.download_tracks(items, Path(self.s["folder"]) / "Mixes" / safe(mx.title), numbered=True)
        elif kind == "artist":
            ar = self.session.artist(ident)
            albums = self._all(lambda lim, off: ar.get_albums(limit=lim, offset=off))
            singles = self._all(lambda lim, off: ar.get_ep_singles(limit=lim, offset=off))
            print(f"Artist: {ar.name} - {len(albums)} albums, {len(singles)} EPs/singles")
            for al in albums + singles:
                if self.cancel.is_set():
                    return
                self.download_album(al)
        elif kind == "video":
            self.download_video(self.session.video(ident))

    @staticmethod
    def _all(fetch, page=100):
        out, off = [], 0
        while True:
            batch = fetch(page, off) or []
            out.extend(batch)
            if len(batch) < page:
                return out
            off += page

    # ---- albums / tracks
    def album_folder(self, album):
        yr = year_of(album)
        name = f"{album.name} ({yr})" if yr else album.name
        return Path(self.s["folder"]) / safe(artists_of(album) or "Unknown") / safe(name)

    def download_album(self, album):
        print(f"\nAlbum: {artists_of(album)} - {album.name} ({album.num_tracks} tracks)")
        tracks = self._all(lambda lim, off: album.tracks(limit=lim, offset=off))
        folder = self.album_folder(album)
        if self.s["covers"]:
            self._save_cover_file(album, folder)
        self.download_tracks(tracks, folder, album=album)

    def download_tracks(self, tracks, folder, album=None, numbered=False):
        total = len(tracks)
        if not total:
            return
        self.single = total == 1
        done = [0]
        self.progress(0, f"Track 0/{total}" if total > 1 else "Downloading")

        def one(n, t):
            if self.cancel.is_set():
                return
            try:
                al = album or t.album
                if al is not None and getattr(al, "num_tracks", -1) in (None, -1, 0):
                    try:  # track lookups only carry a partial album - fetch the full one
                        al = self.session.album(al.id)
                    except Exception:
                        pass
                dest = folder or self.album_folder(al)
                prefix = f"{n:02d}" if numbered else (
                    f"{t.volume_num}-{t.track_num:02d}" if al and (getattr(al, "num_volumes", 1) or 1) > 1
                    else f"{t.track_num:02d}")
                self.download_track(t, al, dest, prefix)
            except Exception as e:
                self.count("failed")
                print(f"  FAILED {title_of(t)}: {e}")
            finally:
                with self._lock:
                    done[0] += 1
                    if total > 1:
                        self.progress(done[0] / total, f"Track {done[0]}/{total}")

        with ThreadPoolExecutor(TRACK_WORKERS) as pool:
            for n, t in enumerate(tracks, 1):
                pool.submit(one, n, t)
        if self.cancel.is_set():
            print("Cancelled.")

    def download_track(self, t, album, folder: Path, prefix):
        base = folder / safe(f"{prefix} - {artists_of(t)} - {title_of(t)}", 180)
        if self.s["skip_existing"]:
            for ext in (".flac", ".m4a", ".mp4"):
                if base.with_suffix(ext).exists():
                    print(f"  skip (exists): {base.name}{ext}")
                    self.count("skipped")
                    return
        self.session.audio_quality = QUALITIES[self.s["quality"]]
        for attempt in range(4):
            try:
                stream = t.get_stream()
                break
            except tidalapi.exceptions.TooManyRequests:
                if attempt == 3:
                    raise
                print(f"  TIDAL says too many requests - waiting {10 * (attempt + 1)} s...")
                time.sleep(10 * (attempt + 1))
        man = stream.get_stream_manifest()
        folder.mkdir(parents=True, exist_ok=True)
        part = base.with_suffix(".part")
        urls = man.get_urls()

        self._fetch(urls, part, title_of(t))
        if self.cancel.is_set():
            part.unlink(missing_ok=True)
            return

        if getattr(man, "encryption_type", "NONE") not in (None, "", "NONE"):
            # TIDALOID does not circumvent DRM: encrypted streams are skipped
            part.unlink(missing_ok=True)
            print(f"  skipped {title_of(t)}: TIDAL sent an encrypted stream")
            self.count("failed")
            return

        codec = (man.get_codecs() or "").upper()
        is_flac = "FLAC" in codec
        if stream.is_mpd and is_flac:
            # DASH FLAC arrives as fragmented MP4: remux to .flac when FFmpeg exists
            ff = ffmpeg_path()
            if ff:
                out = base.with_suffix(".flac")
                r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(part), "-c:a", "copy", str(out)],
                                   capture_output=True, creationflags=0x08000000)
                if r.returncode == 0:
                    part.unlink(missing_ok=True)
                else:
                    print(f"  FFmpeg failed, keeping .m4a: {r.stderr.decode(errors='ignore')[:200]}")
                    out = base.with_suffix(".m4a")
                    part.replace(out)
            else:
                out = base.with_suffix(".m4a")
                part.replace(out)
        else:
            out = base.with_suffix(".flac" if is_flac else ".m4a")
            part.replace(out)

        lyrics = None
        try:
            ly = t.lyrics()
            lyrics = ly.subtitles or ly.text
            if self.s["lyrics"] and ly.subtitles:
                out.with_suffix(".lrc").write_text(ly.subtitles, encoding="utf-8")
        except Exception:
            pass
        try:
            self._tag(out, t, album, lyrics)
        except Exception as e:
            print(f"  (tagging failed: {e})")
        bd, sr = getattr(stream, "bit_depth", None), getattr(stream, "sample_rate", None)
        bits = f"{bd}-bit/{sr / 1000:g} kHz " if bd and sr and is_flac else ""
        print(f"  OK  {out.name}   [{stream.audio_quality} {bits}{codec}]")
        self.count("ok")

    def _get_segment(self, url):
        for attempt in range(4):
            if self.cancel.is_set():
                return b""
            try:
                r = self.http.get(url, timeout=30)
                r.raise_for_status()
                return r.content
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)

    def _fetch(self, urls, dest: Path, label):
        """Download one URL (BTS) or many DASH/HLS segments (in parallel, written in order)."""
        with open(dest, "wb") as f:
            if len(urls) == 1:
                with self.http.get(urls[0], stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    done = 0
                    for chunk in r.iter_content(512 * 1024):
                        if self.cancel.is_set():
                            return
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            self._track_progress(done / total)
            else:
                # map() yields results in order, so segments are written sequentially
                for i, data in enumerate(self.seg_pool.map(self._get_segment, urls)):
                    if self.cancel.is_set():
                        return
                    f.write(data)
                    self._track_progress((i + 1) / len(urls))

    def _track_progress(self, frac):
        if self.single:  # only show per-file progress when one file is downloading
            self.progress(frac, None)

    def _cover_bytes(self, album):
        if album is None:
            return None
        if album.id in self._cover_cache:
            return self._cover_cache[album.id]
        data = None
        try:
            data = requests.get(album.image(1280), timeout=30).content
        except Exception:
            try:
                data = requests.get(album.image(640), timeout=30).content
            except Exception:
                pass
        self._cover_cache[album.id] = data
        return data

    def _save_cover_file(self, album, folder: Path):
        data = self._cover_bytes(album)
        if data:
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / "cover.jpg"
            if not p.exists():
                p.write_bytes(data)

    def _tag(self, path: Path, t, album, lyrics):
        cover = self._cover_bytes(album) if self.s["covers"] else None
        year = str(year_of(album)) if album else ""
        total = getattr(album, "num_tracks", 0) or 0
        if path.suffix == ".flac":
            from mutagen.flac import FLAC, Picture
            a = FLAC(path)
            a["title"] = title_of(t)
            a["artist"] = artists_of(t)
            if album:
                a["album"] = album.name
                a["albumartist"] = artists_of(album)
            a["tracknumber"] = str(t.track_num)
            if total > 0:
                a["tracktotal"] = str(total)
            a["discnumber"] = str(t.volume_num or 1)
            if year:
                a["date"] = year
            if t.isrc:
                a["isrc"] = t.isrc
            if t.copyright:
                a["copyright"] = t.copyright
            if lyrics:
                a["lyrics"] = lyrics
            if cover:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
                a.clear_pictures()
                a.add_picture(pic)
            a.save()
        else:
            from mutagen.mp4 import MP4, MP4Cover
            a = MP4(path)
            a["\xa9nam"] = title_of(t)
            a["\xa9ART"] = artists_of(t)
            if album:
                a["\xa9alb"] = album.name
                a["aART"] = artists_of(album)
            a["trkn"] = [(t.track_num or 0, total if total > 0 else 0)]
            a["disk"] = [(t.volume_num or 1, 0)]
            if year:
                a["\xa9day"] = year
            if t.copyright:
                a["cprt"] = t.copyright
            if lyrics:
                a["\xa9lyr"] = lyrics
            if cover:
                a["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            a.save()

    # ---- video
    def download_video(self, v):
        ff = ffmpeg_path()
        if not ff:
            print(f"Video '{v.name}' skipped: videos need FFmpeg, which is not installed.")
            self.count("failed")
            return
        folder = Path(self.s["folder"]) / "Videos"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / (safe(f"{artists_of(v)} - {title_of(v)}", 180) + ".mp4")
        if self.s["skip_existing"] and out.exists():
            print(f"  skip (exists): {out.name}")
            self.count("skipped")
            return
        self.single = True
        self.progress(0, "Video")
        segs = self._hls_segments(v.get_url())
        ts = out.with_suffix(".ts.part")
        self._fetch(segs, ts, v.name)
        if self.cancel.is_set():
            ts.unlink(missing_ok=True)
            return
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(ts), "-c", "copy", str(out)],
                           capture_output=True, creationflags=0x08000000)
        ts.unlink(missing_ok=True)
        if r.returncode == 0:
            print(f"  OK  {out.name}")
            self.count("ok")
        else:
            print(f"  FAILED video: {r.stderr.decode(errors='ignore')[:200]}")
            self.count("failed")

    @staticmethod
    def _hls_segments(master_url):
        """Resolve an HLS master playlist to the segment URLs of its highest-bandwidth variant."""
        from urllib.parse import urljoin

        def lines(u):
            r = requests.get(u, timeout=30)
            r.raise_for_status()
            return [ln.strip() for ln in r.text.splitlines() if ln.strip()]

        master = lines(master_url)
        best, best_bw = None, -1
        for i, ln in enumerate(master):
            if ln.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", ln)
                bw = int(m.group(1)) if m else 0
                if bw > best_bw and i + 1 < len(master):
                    best, best_bw = urljoin(master_url, master[i + 1]), bw
        media_url = best or master_url
        media = lines(media_url) if best else master
        return [urljoin(media_url, ln) for ln in media if not ln.startswith("#")]


# ---------------------------------------------------------------- GUI
# ------------------------------------------------------------------ theme
# macOS-style light UI (Aqua look, system-blue accent). Tk has no rounded widgets, so the rounded
# pieces are drawn with Pillow (4x supersampled for smooth edges) and registered as ttk image elements.
UI_FONT = "Segoe UI Variable Text"
UI_FONT_DISPLAY = "Segoe UI Variable Display"
C = {
    "root": "#ececec",       # macOS window background
    "card": "#ffffff",       # grouped-content panel
    "card_line": "#d8d8dc",
    "card_hi": "#d8d8dc",
    "field": "#ffffff",
    "field_line": "#c7c7cc",
    "btn": "#ffffff",
    "btn_hover": "#f6f6f8",
    "btn_down": "#e4e4e8",
    "btn_line": "#c4c4c9",
    "btn_dis": "#f4f4f6",
    "btn_dis_line": "#dedee2",
    "text": "#1d1d1f",
    "muted": "#6e6e73",
    "dim": "#aeaeb2",
    "accent": "#007aff",     # macOS system blue
    "accent_hover": "#1a88ff",
    "accent_down": "#0062cc",
    "select": "#007aff",
    "stripe": "#f5f5f7",     # alternating table rows
    "seg_on": "#ffffff",
    "seg_line": "#cfcfd4",
    "seg_hover": "#e2e2e6",
    "thumb": "#c1c1c6",
    "thumb_hover": "#9f9fa5",
    "track": "#d6d6db",
    "error": "#d70015",
    "log_text": "#3a3a3c",
}


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _round_img(w, h, r, fill, outline=None, corner=None, grad=None, line=1, highlight=None, ss=4):
    """Rounded rectangle on an opaque `corner` background (so no alpha tricks are needed in Tk)."""
    from PIL import Image, ImageDraw
    W, H, R = w * ss, h * ss, r * ss
    base = Image.new("RGBA", (W, H), _hex(corner) + (255,) if corner else (0, 0, 0, 0))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=255)
    if grad:
        a, b = _hex(grad[0]), _hex(grad[1])
        g = Image.new("RGBA", (W, 1))
        for x in range(W):
            t = x / max(W - 1, 1)
            g.putpixel((x, 0), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,))
        layer = g.resize((W, H))
    else:
        layer = Image.new("RGBA", (W, H), _hex(fill) + (255,))
    base.paste(layer, (0, 0), mask)
    d = ImageDraw.Draw(base)
    if outline:
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=R, outline=_hex(outline) + (255,), width=line * ss)
        if highlight:
            d.arc([0, 0, 2 * R, 2 * R], 180, 270, fill=_hex(highlight) + (255,), width=line * ss)
            d.arc([W - 1 - 2 * R, 0, W - 1, 2 * R], 270, 360, fill=_hex(highlight) + (255,), width=line * ss)
            d.line([R, 0, W - 1 - R, 0], fill=_hex(highlight) + (255,), width=line * ss)
    return base.resize((w, h), Image.LANCZOS)


def _check_img(on, disabled=False, bg=None, size=18):
    from PIL import Image, ImageDraw
    ss = 4
    img = _round_img(size, size, 5, C["accent"] if on else C["field"],
                     outline=None if on else "#b4b4b9", corner=bg or C["card"], line=1)
    if on:
        big = img.resize((size * ss, size * ss), Image.LANCZOS)
        d = ImageDraw.Draw(big)
        s = size * ss
        d.line([(0.26 * s, 0.53 * s), (0.43 * s, 0.70 * s), (0.75 * s, 0.32 * s)],
               fill=(255, 255, 255, 255), width=int(2.2 * ss), joint="curve")
        img = big.resize((size, size), Image.LANCZOS)
    if disabled:
        from PIL import ImageEnhance
        img = ImageEnhance.Brightness(img).enhance(0.55)
    padded = Image.new("RGBA", (size + 7, size), _hex(bg or C["card"]) + (255,))
    padded.paste(img, (0, 0))
    return padded


def _knob_img(size=18, glow=True):
    from PIL import Image, ImageDraw, ImageFilter
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), _hex(C["card"]) + (255,))
    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(g).ellipse([S * .1, S * .14, S * .9, S * .94], fill=(0, 0, 0, 70 if glow else 30))
    img.alpha_composite(g.filter(ImageFilter.GaussianBlur(S * .05)))
    ImageDraw.Draw(img).ellipse([S * .08, S * .08, S * .92, S * .92], fill=(255, 255, 255, 255),
                                outline=(200, 200, 205, 255), width=ss)
    return img.resize((size, size), Image.LANCZOS)


def _chevron_img(w=22, h=16, bg=None):
    from PIL import Image, ImageDraw
    ss = 4
    img = Image.new("RGBA", (w * ss, h * ss), _hex(bg or C["field"]) + (255,))
    d = ImageDraw.Draw(img)
    cx, cy = w * ss * 0.45, h * ss * 0.5
    d.line([(cx - 4 * ss, cy - 2 * ss), (cx, cy + 2 * ss), (cx + 4 * ss, cy - 2 * ss)],
           fill=_hex(C["muted"]) + (255,), width=int(1.6 * ss), joint="curve")
    return img.resize((w, h), Image.LANCZOS)


def dark_titlebar(win):
    """Windows 11: light title bar tinted to the window colour (macOS-like). Name kept for callers."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi
        off = ctypes.c_int(0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(off), 4)      # light title bar
        r, g, b = _hex(C["root"])
        color = ctypes.c_int(r | (g << 8) | (b << 16))                  # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)    # DWMWA_CAPTION_COLOR
        r, g, b = _hex(C["text"])
        txt = ctypes.c_int(r | (g << 8) | (b << 16))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)      # DWMWA_TEXT_COLOR
    except Exception:
        pass


def apply_theme(root):
    from PIL import ImageTk
    from tkinter import font as tkfont
    global UI_FONT, UI_FONT_DISPLAY
    fams = set(tkfont.families(root))
    if UI_FONT not in fams:
        UI_FONT = UI_FONT_DISPLAY = "Segoe UI"
    elif UI_FONT_DISPLAY not in fams:
        UI_FONT_DISPLAY = UI_FONT
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=UI_FONT, size=12)

    imgs = root._theme_imgs = []

    def tkimg(pil):
        im = ImageTk.PhotoImage(pil, master=root)
        imgs.append(im)
        return im

    s = ttk.Style(root)
    s.theme_use("clam")
    root.configure(background=C["root"])
    s.configure(".", background=C["card"], foreground=C["text"], fieldbackground=C["field"],
                bordercolor=C["card_line"], lightcolor=C["card"], darkcolor=C["card"],
                troughcolor=C["field"], selectbackground=C["select"], selectforeground=C["text"],
                insertcolor=C["text"], font=(UI_FONT, 12), focuscolor=C["card"])
    s.map(".", foreground=[("disabled", C["dim"])])

    # cards
    card = tkimg(_round_img(40, 40, 14, C["card"], outline=C["card_line"], corner=C["root"],
                            highlight=C["card_hi"]))
    s.element_create("Card.bg", "image", card, border=14, sticky="nsew")
    s.layout("Card.TFrame", [("Card.bg", {"sticky": "nsew"})])
    s.configure("Root.TFrame", background=C["root"])
    s.configure("Section.TLabel", background=C["root"], foreground=C["muted"],
                font=(UI_FONT, 10, "bold"))
    s.configure("Title.TLabel", background=C["root"], foreground=C["text"], font=(UI_FONT_DISPLAY, 21, "bold"))
    s.configure("Subtitle.TLabel", background=C["root"], foreground=C["muted"], font=(UI_FONT, 11))
    s.configure("Muted.TLabel", foreground=C["muted"])
    s.configure("Link.TLabel", foreground=C["accent"])
    s.configure("Now.TLabel", foreground=C["accent"])
    s.configure("Error.TLabel", foreground=C["error"])
    s.configure("PreviewTitle.TLabel", font=(UI_FONT_DISPLAY, 15, "bold"))
    s.configure("Time.TLabel", foreground=C["muted"], font=(UI_FONT, 11))

    # buttons: white push button, solid blue default button
    def button_style(name, normal, hover, down, disabled, fg):
        el = f"{name}.bg"
        s.element_create(el, "image", tkimg(normal), ("disabled", tkimg(disabled)),
                         ("pressed", tkimg(down)), ("active", tkimg(hover)), border=(30, 8), padding=(8, 4), sticky="nsew")
        s.layout(f"{name}.TButton", [(el, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure(f"{name}.TButton", padding=(6, 1), width=-6, foreground=fg, anchor="center")
        s.map(f"{name}.TButton", foreground=[("disabled", C["dim"])])

    rr = lambda fill, line=None, grad=None: _round_img(61, 26, 8, fill, outline=line, corner=C["card"], grad=grad)
    dis = rr(C["btn_dis"], C["btn_dis_line"])
    button_style("Mac", rr(C["btn"], C["btn_line"]), rr(C["btn_hover"], C["btn_line"]),
                 rr(C["btn_down"], C["btn_line"]), dis, C["text"])
    button_style("Accent", rr(C["accent"]), rr(C["accent_hover"]), rr(C["accent_down"]), dis, "#ffffff")
    s.layout("TButton", s.layout("Mac.TButton"))
    s.configure("TButton", padding=(6, 1), width=-6, foreground=C["text"], anchor="center")
    s.map("TButton", foreground=[("disabled", C["dim"])])

    # text fields + combobox
    fld = lambda line, w=1: _round_img(36, 26, 8, C["field"], outline=line, corner=C["card"], line=w)
    s.element_create("Mac.field", "image", tkimg(fld(C["field_line"])), ("focus", tkimg(fld(C["accent"], 2))),
                     ("disabled", tkimg(fld(C["btn_dis_line"]))), border=9, padding=(6, 4), sticky="nsew")
    s.layout("TEntry", [("Mac.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TEntry", padding=(9, 3), foreground=C["text"])
    s.element_create("Mac.chevron", "image", tkimg(_chevron_img()), sticky="")
    s.layout("TCombobox", [("Mac.field", {"sticky": "nsew", "children": [
        ("Mac.chevron", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
            ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TCombobox", padding=(9, 3), foreground=C["text"], arrowsize=0)
    s.map("TCombobox", fieldbackground=[("readonly", C["field"])], foreground=[("readonly", C["text"])],
          selectbackground=[("readonly", C["field"])], selectforeground=[("readonly", C["text"])])
    root.option_add("*TCombobox*Listbox.background", C["field"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", (UI_FONT, 12))
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*Menu.background", C["card"])
    root.option_add("*Menu.foreground", C["text"])
    root.option_add("*Menu.activeBackground", C["accent"])
    root.option_add("*Menu.activeForeground", "#ffffff")
    root.option_add("*Toplevel.background", C["root"])

    # checkboxes
    s.element_create("Mac.check", "image", tkimg(_check_img(False)),
                     ("disabled", "selected", tkimg(_check_img(True, True))), ("disabled", tkimg(_check_img(False, True))),
                     ("selected", tkimg(_check_img(True))), sticky="")
    s.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
        ("Mac.check", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])
    s.configure("TCheckbutton", padding=(2, 2), foreground=C["text"])
    s.map("TCheckbutton", background=[("active", C["card"])])

    # segmented tabs (notebook)
    seg_on = tkimg(_round_img(40, 30, 9, C["seg_on"], outline=C["seg_line"], corner=C["root"]))
    seg_hover = tkimg(_round_img(40, 30, 9, C["seg_hover"], corner=C["root"]))
    seg_off = tkimg(_round_img(40, 30, 9, C["root"], corner=C["root"]))
    s.element_create("Mac.tab", "image", seg_off, ("selected", seg_on), ("active", seg_hover), border=9, sticky="nsew")
    s.layout("TNotebook.Tab", [("Mac.tab", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"sticky": "nsew", "children": [("Notebook.label", {"sticky": ""})]})]})])
    s.configure("TNotebook", background=C["root"], borderwidth=0, tabmargins=(0, 0, 0, 6))
    s.configure("TNotebook.Tab", padding=(16, 3), foreground=C["muted"], font=(UI_FONT, 12, "bold"))
    s.map("TNotebook.Tab", foreground=[("selected", C["text"]), ("active", C["text"])],
          expand=[("selected", (0, 0, 0, 0))])
    s.element_create("Mac.client", "image", card, border=14, sticky="nsew")
    s.layout("TNotebook", [("Mac.client", {"sticky": "nsew"})])

    # results table
    s.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["text"],
                borderwidth=0, relief="flat", font=(UI_FONT, 12))
    s.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", "#ffffff")])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
    s.configure("Treeview.Heading", background=C["card"], foreground=C["muted"], relief="flat",
                borderwidth=0, font=(UI_FONT, 11, "bold"), padding=(6, 6))
    s.map("Treeview.Heading", background=[("active", C["btn"])], foreground=[("active", C["text"])])

    # solid base under slim widgets (otherwise Tk's default grey shows around them)
    from PIL import Image as _Img
    s.element_create("Mac.base", "image", tkimg(_Img.new("RGBA", (6, 6), _hex(C["card"]) + (255,))),
                     border=2, sticky="nsew")

    # slim scrollbars
    thumb = tkimg(_round_img(8, 30, 4, C["thumb"], corner=C["card"]))
    thumb_h = tkimg(_round_img(8, 30, 4, C["thumb_hover"], corner=C["card"]))
    trough = tkimg(_round_img(8, 30, 4, C["card"], corner=C["card"]))
    s.element_create("Mac.Vertical.Scrollbar.trough", "image", trough, border=4, sticky="ns")
    s.element_create("Mac.Vertical.Scrollbar.thumb", "image", thumb, ("active", thumb_h), ("pressed", thumb_h),
                     border=4, sticky="nsew")
    s.layout("Vertical.TScrollbar", [("Mac.base", {"sticky": "nsew", "children": [
        ("Mac.Vertical.Scrollbar.trough", {"sticky": "nsew", "children": [
            ("Mac.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nsew"})]})]})])
    s.configure("Vertical.TScrollbar", arrowsize=0, width=8)

    # progress bar: blue pill
    s.element_create("Mac.Horizontal.Progressbar.trough", "image",
                     tkimg(_round_img(30, 8, 4, "#e5e5ea", corner=C["card"])),
                     border=4, sticky="nsew")
    s.element_create("Mac.Horizontal.Progressbar.pbar", "image",
                     tkimg(_round_img(30, 8, 4, C["accent"], corner="#e5e5ea")), border=4, sticky="nsew")
    s.layout("Horizontal.TProgressbar", [("Mac.base", {"sticky": "nsew", "children": [
        ("Mac.Horizontal.Progressbar.trough", {"sticky": "nsew", "children": [
            ("Mac.Horizontal.Progressbar.pbar", {"side": "left", "sticky": "ns"})]})]})])
    s.configure("Horizontal.TProgressbar", thickness=8)

    # seek slider: thin track + white knob
    track = _round_img(30, 4, 2, C["track"], corner=C["card"])
    from PIL import Image
    tr = Image.new("RGBA", (30, 18), _hex(C["card"]) + (255,))
    tr.paste(track, (0, 7))
    s.element_create("Mac.Horizontal.Scale.trough", "image", tkimg(tr), border=(4, 0), sticky="ew")
    s.element_create("Mac.Horizontal.Scale.slider", "image", tkimg(_knob_img()),
                     ("disabled", tkimg(_knob_img(glow=False))), sticky="")
    s.layout("Horizontal.TScale", [("Mac.Horizontal.Scale.trough", {"sticky": "ew", "children": [
        ("Mac.Horizontal.Scale.slider", {"side": "left", "sticky": ""})]})])
    s.configure("Horizontal.TScale", sliderlength=18)
    dark_titlebar(root)


class GradientBar(tk.Canvas):
    """Thin divider line that stretches with the window."""

    def __init__(self, master, height=2, colors=None, bg=None):
        super().__init__(master, height=height, highlightthickness=0, bd=0, background=bg or C["root"])
        self.colors = colors or (C["card_line"], C["card_line"])
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        stops = [_hex(c) for c in self.colors]
        steps = max(w // 3, 1)
        for i in range(steps):
            t = i / max(steps - 1, 1) * (len(stops) - 1)
            a, b = stops[min(int(t), len(stops) - 2)], stops[min(int(t), len(stops) - 2) + 1]
            f = t - min(int(t), len(stops) - 2)
            col = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))
            x = i * w / steps
            self.create_rectangle(x, 0, x + w / steps + 1, h, fill=col, width=0)


class App(tk.Tk):
    def __init__(self):
        try:  # own taskbar identity, so Windows shows our icon instead of python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TIDALOID.App")
        except Exception:
            pass
        super().__init__()
        self.title(APP_TITLE)
        for icon in app_icon_candidates():
            if icon.exists():
                try:
                    self.iconbitmap(default=str(icon))   # default= also applies to dialogs
                    break
                except tk.TclError:
                    pass
        self.geometry("1300x1000")
        self.minsize(820, 580)
        self.settings = load_settings()
        self.session = tidalapi.Session()
        self.busy = False
        self.state = "checking"
        self.cancel = threading.Event()
        self.login_cancel = threading.Event()
        self._build()
        self.after(100, self._pump_log)
        self._set_state("checking")
        threading.Thread(target=self._auto_login, daemon=True).start()
        import atexit
        atexit.register(self.player.stop)   # never leave ffplay playing after the app is gone

    def destroy(self):
        try:
            self.player.stop()
        except Exception:
            pass
        super().destroy()

    # ---- layout
    def _build(self):
        apply_theme(self)

        # header: app title + account status/buttons
        head = ttk.Frame(self, style="Root.TFrame")
        head.pack(fill="x", padx=18, pady=(10, 0))
        titles = ttk.Frame(head, style="Root.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="TIDALOID", style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar()
        ttk.Label(titles, textvariable=self.status_var, style="Subtitle.TLabel").pack(anchor="w")
        acc = ttk.Frame(head, style="Card.TFrame", padding=8)
        acc.pack(side="right")
        self.logout_btn = ttk.Button(acc, text="Sign out", command=self._logout)
        self.logout_btn.pack(side="right", padx=(4, 0))
        self.pkce_btn = ttk.Button(acc, text="Lossless sign-in", style="Accent.TButton", command=self._pkce_login)
        self.pkce_btn.pack(side="right", padx=4)
        self.quick_btn = ttk.Button(acc, text="Quick sign-in", command=self._quick_login)
        self.quick_btn.pack(side="right", padx=(0, 4))
        GradientBar(self).pack(fill="x", padx=18, pady=(8, 2))
        self.link_lbl = ttk.Label(titles, style="Link.TLabel", cursor="hand2")
        self.link_lbl.configure(background=C["root"])
        self.link_lbl.bind("<Button-1>", lambda e: self.login_url and webbrowser.open(self.login_url))
        self.login_url = ""

        pad = {"padx": 18, "pady": 5}
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(6, 5))
        self._build_search_tab()
        lf = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(lf, text="Links")
        ttk.Label(lf, text="TIDAL links (album, track, playlist, artist, video or mix) - one per line:",
                  style="Muted.TLabel").pack(anchor="w", padx=8, pady=(8, 6))
        self.links = tk.Text(lf, height=5, wrap="none", font=(UI_FONT, 12), **self._text_opts())
        self.links.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.links.bind("<Button-3>", self._paste_menu)
        self.dl_btn = ttk.Button(lf, text="Download links", style="Accent.TButton", command=self._download_links)
        self.dl_btn.pack(anchor="w", padx=8, pady=(0, 8))

        opt = self._card("OPTIONS", fill="x", **pad)
        ttk.Label(opt, text="Audio quality", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=8, pady=5)
        self.aq = ttk.Combobox(opt, state="readonly", width=24, values=list(QUALITIES))
        self.aq.set(self.settings["quality"])
        self.aq.grid(row=0, column=1, sticky="w")
        self.aq.bind("<<ComboboxSelected>>", lambda e: self._save_settings())
        self.lyrics_var = tk.BooleanVar(value=self.settings["lyrics"])
        ttk.Checkbutton(opt, text="Save lyrics (.lrc)", variable=self.lyrics_var,
                        command=self._save_settings).grid(row=0, column=2, sticky="w", padx=(20, 8))
        self.covers_var = tk.BooleanVar(value=self.settings["covers"])
        ttk.Checkbutton(opt, text="Cover art", variable=self.covers_var,
                        command=self._save_settings).grid(row=0, column=3, sticky="w", padx=8)
        self.skip_var = tk.BooleanVar(value=self.settings["skip_existing"])
        ttk.Checkbutton(opt, text="Skip existing files", variable=self.skip_var,
                        command=self._save_settings).grid(row=0, column=4, sticky="w", padx=8)
        ttk.Label(opt, text="Download folder", style="Muted.TLabel").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        self.path_var = tk.StringVar(value=self.settings["folder"])
        pe = ttk.Entry(opt, textvariable=self.path_var)
        pe.grid(row=1, column=1, columnspan=4, sticky="ew")
        pe.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(opt, text="Browse…", command=self._browse).grid(row=1, column=5, sticky="w", padx=8)
        opt.columnconfigure(4, weight=1)

        logf = self._card("ACTIVITY", fill="both", padx=18, pady=(5, 16))
        act = ttk.Frame(logf)
        act.pack(fill="x", padx=4, pady=(0, 8))
        ttk.Button(act, text="Open folder", command=self._open_folder).pack(side="left")
        ttk.Button(act, text="Clear log", command=self._log_clear).pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(act, text="Cancel download", command=self._cancel_download)
        self.cancel_btn.state(["disabled"])
        self.cancel_btn.pack(side="left")
        self.progress = ttk.Progressbar(act, mode="determinate", length=240, maximum=1.0)
        self.progress.pack(side="right", padx=(0, 4))
        self.work_var = tk.StringVar()
        ttk.Label(act, textvariable=self.work_var, style="Muted.TLabel").pack(side="right", padx=10)
        logbox = ttk.Frame(logf)
        logbox.pack(fill="both", expand=True, padx=4)
        self.log = tk.Text(logbox, height=4, state="disabled", wrap="word", font=("Cascadia Mono", 11),
                           **self._text_opts())
        sb = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(4, 0))
        self.log.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _card(self, title, **pack):
        """Section caption + rounded card; returns the card frame (already packed)."""
        outer = ttk.Frame(self, style="Root.TFrame")
        outer.pack(**pack)
        ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w", padx=6, pady=(0, 3))
        card = ttk.Frame(outer, style="Card.TFrame", padding=(12, 9))
        card.pack(fill="both", expand=True)
        return card

    @staticmethod
    def _text_opts():
        return dict(background=C["field"], foreground=C["log_text"], insertbackground=C["text"],
                    selectbackground=C["select"], selectforeground="#ffffff", relief="flat", borderwidth=0,
                    highlightthickness=1, highlightbackground=C["field_line"], highlightcolor=C["accent"],
                    padx=10, pady=8)

    def _build_search_tab(self):
        tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(tab, text="Search")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=8)
        self.q_var = tk.StringVar()
        q = ttk.Entry(bar, textvariable=self.q_var, font=(UI_FONT, 12))
        q.pack(side="left", fill="x", expand=True)
        q.bind("<Return>", lambda e: self._search(0))
        self.stype = ttk.Combobox(bar, state="readonly", width=10, values=list(SEARCH_TYPES))
        self.stype.set("Tracks")
        self.stype.pack(side="left", padx=6)
        self.stype.bind("<<ComboboxSelected>>", lambda e: self.q_var.get().strip() and self._search(0))
        self.search_btn = ttk.Button(bar, text="Search", style="Accent.TButton", command=lambda: self._search(0))
        self.search_btn.pack(side="left")

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=8)

        # preview pane (right)
        prev = ttk.Frame(body, width=PREVIEW_SIZE + 64)
        prev.pack(side="right", fill="y", padx=(8, 0))
        prev.pack_propagate(False)
        self._blank_big = tk.PhotoImage(width=PREVIEW_SIZE, height=PREVIEW_SIZE)
        self.prev_img = tk.Label(prev, image=self._blank_big, background=C["field"], borderwidth=0,
                                 width=PREVIEW_SIZE, height=PREVIEW_SIZE)
        self.prev_img.pack(pady=(0, 6))
        self.prev_title = ttk.Label(prev, style="PreviewTitle.TLabel", wraplength=PREVIEW_SIZE + 48, justify="left")
        self.prev_title.pack(anchor="w")
        self.prev_sub = ttk.Label(prev, wraplength=PREVIEW_SIZE + 48, justify="left")
        self.prev_sub.pack(anchor="w", pady=(2, 0))
        self.prev_info = ttk.Label(prev, wraplength=PREVIEW_SIZE + 48, justify="left", style="Muted.TLabel")
        self.prev_info.pack(anchor="w", pady=(2, 0))
        self.prev_play_btn = ttk.Button(prev, text="▶  Play preview", style="Accent.TButton", command=self._toggle_play)
        self.prev_play_btn.pack(fill="x", pady=(8, 0))
        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(prev, from_=0.0, to=1.0, orient="horizontal", variable=self.seek_var)
        self.seek_scale.pack(fill="x", pady=(6, 0))
        seekrow = ttk.Frame(prev)
        seekrow.pack(fill="x", pady=(2, 0))
        self.rew_btn = ttk.Button(seekrow, text="« 10s", command=lambda: self._seek_rel(-10))
        self.rew_btn.pack(side="left")
        self.fwd_btn = ttk.Button(seekrow, text="10s »", command=lambda: self._seek_rel(+10))
        self.fwd_btn.pack(side="right")
        self.pos_var = tk.StringVar(value="")
        ttk.Label(seekrow, textvariable=self.pos_var, anchor="center", style="Time.TLabel").pack(
            side="left", fill="x", expand=True)
        # Own mouse handling: ttk's default trough click steps by 1 (= the whole track) instead of
        # jumping to the click point. Click or drag puts the knob under the mouse; release seeks.
        self.seek_scale.bind("<ButtonPress-1>", self._seek_drag)
        self.seek_scale.bind("<B1-Motion>", self._seek_drag)
        self.seek_scale.bind("<ButtonRelease-1>", self._seek_release)
        self._dragging = False
        self.now_var = tk.StringVar()
        ttk.Label(prev, textvariable=self.now_var, wraplength=PREVIEW_SIZE + 48, justify="left",
                  style="Now.TLabel").pack(anchor="w", pady=(6, 0))
        self.player = Player(lambda playing, text: self._ui(self._player_state, playing, text))
        self.playing = False

        style = ttk.Style(self)
        style.configure("Results.Treeview", rowheight=THUMB_SIZE + 6)
        cols = ("title", "artist", "info", "length", "quality")
        self.results = ttk.Treeview(body, columns=cols, show="tree headings", selectmode="extended",
                                    height=6, style="Results.Treeview")
        self.results.column("#0", width=THUMB_SIZE + 20, stretch=False, anchor="center")
        self.results.heading("#0", text="")
        for c, text, w, stretch in (("title", "Title", 220, True), ("artist", "Artist", 150, True),
                                    ("info", "Album / info", 160, True), ("length", "Length", 74, False),
                                    ("quality", "Quality", 86, False)):
            self.results.heading(c, text=text)
            self.results.column(c, width=w, stretch=stretch, anchor="w")
        rsb = ttk.Scrollbar(body, command=self.results.yview)
        self.results.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")
        self.results.pack(fill="both", expand=True)
        self.results.tag_configure("stripe", background=C["stripe"])
        self.results.bind("<Double-1>", lambda e: self._download_selected())
        self.results.bind("<Return>", lambda e: self._download_selected())
        self.results.bind("<<TreeviewSelect>>", lambda e: (self._refresh_buttons(), self._show_preview()))
        self.results.bind("<space>", lambda e: (self._toggle_play(), "break")[1])
        self.results.bind("<Left>", lambda e: (self._seek_rel(-10), "break")[1] if self.player.active() else None)
        self.results.bind("<Right>", lambda e: (self._seek_rel(+10), "break")[1] if self.player.active() else None)

        self._blank_thumb = tk.PhotoImage(width=THUMB_SIZE, height=THUMB_SIZE)
        self._img_bytes = {}      # url -> jpeg bytes (shared cache)
        self._thumbs = {}         # url -> PhotoImage (keep references!)
        self._bigs = {}           # url -> PhotoImage
        self._result_meta = {}    # iid -> (small_url, big_url, title, subtitle, info)
        self._search_gen = 0
        self._img_pool = ThreadPoolExecutor(6)

        nav = ttk.Frame(tab)
        nav.pack(fill="x", padx=8, pady=8)
        self.dl_sel_btn = ttk.Button(nav, text="Download selected", style="Accent.TButton", command=self._download_selected)
        self.dl_sel_btn.pack(side="left")
        self.play_btn = ttk.Button(nav, text="▶ Play", width=9, command=self._toggle_play)
        self.play_btn.pack(side="left", padx=(6, 0))
        ttk.Button(nav, text="Copy links", command=self._copy_selected_links).pack(side="left", padx=6)
        self.next_btn = ttk.Button(nav, text="Next ▶", width=8, command=lambda: self._search(+1))
        self.next_btn.pack(side="right")
        self.prev_btn = ttk.Button(nav, text="◀ Prev", width=8, command=lambda: self._search(-1))
        self.prev_btn.pack(side="right", padx=4)
        self.page_var = tk.StringVar(value="Type a song, album or artist and press Enter.")
        ttk.Label(nav, textvariable=self.page_var).pack(side="right", padx=8)
        self.prev_btn.state(["disabled"])
        self.next_btn.state(["disabled"])
        self.search_offset = 0
        self.search_query = ("", "Tracks")
        self.result_urls = {}

    # ---- small helpers
    def _ui(self, fn, *args):
        self.after(0, lambda: fn(*args))

    def _paste_menu(self, event):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Paste", command=lambda: self.links.event_generate("<<Paste>>"))
        m.add_command(label="Clear", command=lambda: self.links.delete("1.0", "end"))
        m.tk_popup(event.x_root, event.y_root)

    def _pump_log(self):
        chunks = []
        try:
            while True:
                chunks.append(LOG_Q.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.log.configure(state="normal")
            self.log.insert("end", "".join(chunks).replace("\r", "\n"))
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(100, self._pump_log)

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _save_settings(self):
        self.settings.update(quality=self.aq.get(), lyrics=self.lyrics_var.get(),
                             covers=self.covers_var.get(), skip_existing=self.skip_var.get(),
                             folder=self.path_var.get().strip() or DEFAULT_SETTINGS["folder"])
        save_settings(self.settings)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or str(HOME))
        if d:
            self.path_var.set(os.path.normpath(d))
            self._save_settings()

    def _open_folder(self):
        self._save_settings()
        p = Path(self.settings["folder"])
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    # ---- state
    def _set_state(self, state, detail=""):
        self.state = state
        self.link_lbl.pack_forget()
        btns_login = ["!disabled"] if state in ("signed_out", "signed_in") else ["disabled"]
        self.pkce_btn.state(btns_login)
        self.quick_btn.configure(text="Quick sign-in")
        self.quick_btn.state(btns_login)
        self.logout_btn.state(["!disabled"] if state == "signed_in" else ["disabled"])
        if state == "checking":
            self.status_var.set("Checking saved sign-in...")
        elif state == "signed_out":
            self.status_var.set("Not signed in." + (f"  ({detail})" if detail else ""))
        elif state == "waiting":
            self.status_var.set("Approve the sign-in in your browser:")
            self.link_lbl.configure(text=self.login_url)
            self.link_lbl.pack(anchor="w")
            self.quick_btn.configure(text="Cancel")
            self.quick_btn.state(["!disabled"])
        elif state == "signed_in":
            kind = "Lossless sign-in - FLAC available" if self.session.is_pkce \
                else "Quick sign-in - max 320 kbps AAC"
            self.status_var.set(f"Signed in (user {self.session.user.id}, {self.session.country_code})  -  {kind}")
        self._refresh_buttons()

    def _refresh_buttons(self):
        ok = self.state == "signed_in" and not self.busy
        self.dl_btn.state(["!disabled"] if ok else ["disabled"])
        self.dl_sel_btn.state(["!disabled"] if ok and self.results.selection() else ["disabled"])
        self.search_btn.state(["!disabled"] if self.state == "signed_in" else ["disabled"])
        can_play = self.playing or (self.state == "signed_in" and bool(self.results.selection()))
        for b in (self.play_btn, self.prev_play_btn):
            b.state(["!disabled"] if can_play else ["disabled"])
        can_seek = self.playing and self.player.active()
        for b in (self.rew_btn, self.fwd_btn, self.seek_scale):
            b.state(["!disabled"] if can_seek else ["disabled"])
        self.cancel_btn.state(["!disabled"] if self.busy else ["disabled"])

    # ---- login
    def _save_session(self):
        try:
            self.session.save_session_to_file(SESSION_FILE)
        except Exception as e:
            print(f"Could not save sign-in: {e}")

    def _auto_login(self):
        ok = False
        try:
            if SESSION_FILE.exists():
                ok = bool(self.session.load_session_from_file(SESSION_FILE)) and self.session.check_login()
                if ok:
                    self._save_session()   # persists refreshed tokens
                    print("Signed in from saved session.")
        except Exception as e:
            print(f"Saved sign-in could not be used: {e}")
        self._ui(self._set_state, "signed_in" if ok else "signed_out")

    def _quick_login(self):
        if self.state == "waiting":
            self.login_cancel.set()
            return
        self.login_cancel.clear()
        self.quick_btn.state(["disabled"])
        threading.Thread(target=self._quick_worker, daemon=True).start()

    def _quick_worker(self):
        try:
            self.session = tidalapi.Session()
            login, future = self.session.login_oauth()
            self.login_url = "https://" + login.verification_uri_complete
            self._ui(self._set_state, "waiting")
            webbrowser.open(self.login_url)
            print(f"Approve the sign-in: {self.login_url}")
            while not future.done():
                if self.login_cancel.wait(1):
                    print("Sign-in cancelled.")
                    self._ui(self._set_state, "signed_out")
                    return
            future.result()
            if self.session.check_login():
                self._save_session()
                print("Signed in (quick sign-in: TIDAL limits these sessions to 320 kbps AAC).")
                self._ui(self._set_state, "signed_in")
            else:
                self._ui(self._set_state, "signed_out", "sign-in failed")
        except Exception as e:
            print(f"Sign-in failed: {e}")
            self._ui(self._set_state, "signed_out", "sign-in failed - see log")

    def _pkce_login(self):
        sess = tidalapi.Session()
        url = sess.pkce_login_url()
        dlg = tk.Toplevel(self)
        dlg.title("Lossless sign-in")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(background=C["card"])
        dark_titlebar(dlg)
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        steps = ("1.  Click 'Open TIDAL sign-in' and sign in with your TIDAL account.\n"
                 "2.  After signing in, the browser shows an 'Oops' / page-not-found page.\n"
                 "    That is expected.\n"
                 "3.  Copy the FULL address of that page from the browser's address bar\n"
                 "    (click the address bar, Ctrl+A, Ctrl+C) and paste it below.")
        ttk.Label(frm, text=steps, justify="left").pack(anchor="w")
        ttk.Button(frm, text="Open TIDAL sign-in", command=lambda: webbrowser.open(url)).pack(anchor="w", pady=10)
        ttk.Label(frm, text="Address of the 'Oops' page:").pack(anchor="w")
        var = tk.StringVar()
        ent = ttk.Entry(frm, textvariable=var, width=80)
        ent.pack(fill="x", pady=4)
        msg = tk.StringVar()
        ttk.Label(frm, textvariable=msg, style="Error.TLabel").pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(8, 0))

        def finish():
            u = var.get().strip()
            if "code=" not in u:
                msg.set("That address has no 'code=' in it - copy the whole address of the Oops page.")
                return
            try:
                tok = sess.pkce_get_auth_token(u)
                sess.process_auth_token(tok, is_pkce_token=True)
                if not sess.check_login():
                    raise Exception("TIDAL did not accept the sign-in")
            except Exception as e:
                msg.set(f"Sign-in failed: {str(e)[:120]}")
                return
            self.session = sess
            self._save_session()
            print("Signed in (lossless sign-in: FLAC / Hi-Res available).")
            dlg.destroy()
            self._set_state("signed_in")

        ttk.Button(row, text="Sign in", command=finish).pack(side="right")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)
        ent.bind("<Return>", lambda e: finish())
        webbrowser.open(url)
        ent.focus_set()

    def _logout(self):
        if not messagebox.askyesno(APP_TITLE, "Sign out and forget the saved sign-in?"):
            return
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self.session = tidalapi.Session()
        print("Signed out.")
        self._set_state("signed_out")

    # ---- search
    def _search(self, direction):
        if self.state != "signed_in":
            messagebox.showinfo(APP_TITLE, "Sign in first.")
            return
        if direction == 0:
            text = self.q_var.get().strip()
            if not text:
                return
            self.search_query = (text, self.stype.get())
            self.search_offset = 0
        else:
            self.search_offset = max(0, self.search_offset + direction * PAGE_SIZE)
        text, label = self.search_query
        self.search_btn.state(["disabled"])
        self.page_var.set("Searching...")
        threading.Thread(target=self._search_worker, args=(text, label, self.search_offset), daemon=True).start()

    def _search_worker(self, text, label, offset):
        try:
            res = self.session.search(text, models=[SEARCH_TYPES[label]], limit=PAGE_SIZE, offset=offset)
            items = res.get(RESULT_KEY[label]) or []
            rows = []
            for it in items:
                vals, url = self._row(label, it)
                small, big = image_urls(label, it)
                rows.append((vals, url, small, big))
            self._ui(self._show_results, rows, offset)
        except Exception as e:
            print(f"Search failed: {e}")
            self._ui(self._show_results, [], 0, "Search failed - see log")

    @staticmethod
    def _row(label, it):
        if label == "Tracks":
            return ((title_of(it), artists_of(it), getattr(it.album, "name", "") or "",
                     fmt_time(it.duration), quality_label(it)), f"https://tidal.com/browse/track/{it.id}")
        if label == "Albums":
            yr = year_of(it)
            return ((title_of(it), artists_of(it), f"{plural(it.num_tracks, 'track')}  {yr}".strip(),
                     fmt_time(it.duration), quality_label(it)), f"https://tidal.com/browse/album/{it.id}")
        if label == "Artists":
            return ((it.name, "", "Artist - downloads all albums", "", ""),
                    f"https://tidal.com/browse/artist/{it.id}")
        if label == "Playlists":
            creator = getattr(getattr(it, "creator", None), "name", "") or ""
            return ((it.name, creator, plural(it.num_tracks, "track"), fmt_time(it.duration), ""),
                    f"https://tidal.com/browse/playlist/{it.id}")
        return ((title_of(it), artists_of(it), "Video", fmt_time(it.duration),
                 str(getattr(it, "video_quality", "") or "").replace("MP4_", "").lower()),
                f"https://tidal.com/browse/video/{it.id}")

    def _show_results(self, rows, offset, msg=None):
        self._search_gen += 1
        gen = self._search_gen
        self.results.delete(*self.results.get_children())
        self.result_urls.clear()
        self._result_meta.clear()
        self._clear_preview()
        for vals, url, small, big in rows:
            img = self._thumbs.get(small, self._blank_thumb)
            iid = self.results.insert("", "end", values=vals, image=img,
                                      tags=("stripe",) if len(self.result_urls) % 2 else ())
            self.result_urls[iid] = url
            self._result_meta[iid] = (small, big, vals[0], vals[1], " • ".join(v for v in vals[2:] if v))
            if small and small not in self._thumbs:
                self._img_pool.submit(self._load_thumb, gen, iid, small)
        if rows:
            first = self.results.get_children()[0]
            self.results.selection_set(first)
            self.results.focus(first)
        if msg:
            self.page_var.set(msg)
        elif not rows:
            self.page_var.set("No results.")
        else:
            self.page_var.set(f"Results {offset + 1}-{offset + len(rows)}")
        self.prev_btn.state(["!disabled"] if offset > 0 else ["disabled"])
        more = len(rows) == PAGE_SIZE and offset + PAGE_SIZE < SEARCH_LIMIT
        self.next_btn.state(["!disabled"] if more else ["disabled"])
        self._refresh_buttons()

    # ---- cover images
    def _image_bytes(self, url):
        data = self._img_bytes.get(url)
        if data is None:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            data = self._img_bytes[url] = r.content
        return data

    def _pil_image(self, url, size):
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(self._image_bytes(url))).convert("RGB")
        return ImageOps.fit(img, (size, size), Image.LANCZOS)

    def _load_thumb(self, gen, iid, url):
        try:
            pil = self._pil_image(url, THUMB_SIZE)
        except Exception:
            return

        def apply():
            from PIL import ImageTk
            if url not in self._thumbs:
                self._thumbs[url] = ImageTk.PhotoImage(pil)
            # apply to every visible row using this cover (albums share covers across tracks)
            if gen == self._search_gen:
                for i, meta in self._result_meta.items():
                    if meta[0] == url and self.results.exists(i):
                        self.results.item(i, image=self._thumbs[url])
        self.after(0, apply)

    def _clear_preview(self):
        self.prev_img.configure(image=self._blank_big)
        self.prev_title.configure(text="")
        self.prev_sub.configure(text="")
        self.prev_info.configure(text="")

    def _show_preview(self):
        sel = self.results.selection()
        if not sel or sel[-1] not in self._result_meta:
            return
        iid = sel[-1]
        small, big, title, sub, info = self._result_meta[iid]
        self.prev_title.configure(text=title)
        self.prev_sub.configure(text=sub)
        self.prev_info.configure(text=info)
        self._preview_iid = iid
        if big in self._bigs:
            self.prev_img.configure(image=self._bigs[big])
            return
        self.prev_img.configure(image=self._blank_big)
        if big:
            self._img_pool.submit(self._load_big, iid, big)

    def _load_big(self, iid, url):
        try:
            pil = self._pil_image(url, PREVIEW_SIZE)
        except Exception:
            return

        def apply():
            from PIL import ImageTk
            if url not in self._bigs:
                self._bigs[url] = ImageTk.PhotoImage(pil)
            if getattr(self, "_preview_iid", None) == iid:
                self.prev_img.configure(image=self._bigs[url])
        self.after(0, apply)

    # ---- preview playback
    def _toggle_play(self):
        if self.playing:
            self.player.stop()
            self._player_state(False, "")
            return
        if self.state != "signed_in":
            return
        sel = self.results.selection()
        if not sel or sel[-1] not in self.result_urls:
            return
        iid = sel[-1]
        m = URL_RE.search(self.result_urls[iid])
        if not m:
            return
        kind, ident = m.group(1).lower(), m.group(2)
        if kind == "video":
            self.now_var.set("Videos can't be previewed - only music.")
            return

        def resolve(sess):
            if kind == "track":
                return sess.track(ident)
            if kind == "album":
                tr = sess.album(ident).tracks(limit=1)
            elif kind == "playlist":
                tr = sess.playlist(ident).tracks(limit=1)
            else:  # artist -> most popular track
                tr = sess.artist(ident).get_top_tracks(limit=1)
            return tr[0] if tr else None

        self.player.play(self.session, resolve, self._result_meta.get(iid, ("", "", "this"))[2])

    def _player_state(self, playing, text):
        was = self.playing
        self.playing = playing
        label = "■ Stop" if playing else "▶ Play"
        self.play_btn.configure(text=label)
        self.prev_play_btn.configure(text="■  Stop" if playing else "▶  Play preview")
        self.now_var.set(text)
        if text.startswith("Preview failed"):
            print(text)
        if not playing:
            self.pos_var.set("")
            self.seek_var.set(0.0)
        elif not was:
            self.after(250, self._tick)
        self._refresh_buttons()

    def _tick(self):
        """Update the position display while a preview plays."""
        if not self.playing:
            return
        if self.player.active():
            pos, dur = self.player.position(), self.player.duration
            mmss = lambda s: f"{int(s) // 60}:{int(s) % 60:02}"
            self.pos_var.set(f"{mmss(pos)} / {mmss(dur)}")
            if not self._dragging and dur:
                self.seek_var.set(pos / dur)
        self._refresh_buttons()
        self.after(250, self._tick)

    def _seek_rel(self, delta):
        if self.playing and self.player.active():
            self.player.seek(self.player.position() + delta)

    def _seek_fraction(self, event):
        """0..1 position under the mouse on the seek bar."""
        w = self.seek_scale.winfo_width()
        knob = 18                      # knob width (sliderlength in the theme); the knob centre can't reach the ends
        return max(0.0, min(1.0, (event.x - knob / 2) / max(w - knob, 1)))

    def _seek_drag(self, event):
        if not (self.playing and self.player.active()):
            return "break"
        self._dragging = True
        frac = self._seek_fraction(event)
        self.seek_var.set(frac)
        mmss = lambda s: f"{int(s) // 60}:{int(s) % 60:02}"
        self.pos_var.set(f"{mmss(frac * self.player.duration)} / {mmss(self.player.duration)}")
        return "break"

    def _seek_release(self, event):
        if not self._dragging:
            return "break"
        self._dragging = False
        if self.playing and self.player.active() and self.player.duration:
            self.player.seek(self._seek_fraction(event) * self.player.duration)
        return "break"

    def _selected_urls(self):
        return [self.result_urls[i] for i in self.results.selection() if i in self.result_urls]

    def _copy_selected_links(self):
        urls = self._selected_urls()
        if urls:
            self.clipboard_clear()
            self.clipboard_append("\n".join(urls))
            print(f"Copied {len(urls)} link(s) to the clipboard.")

    # ---- download
    def _download_selected(self):
        urls = self._selected_urls()
        if not urls or self.busy or self.state != "signed_in":
            return
        if any("/artist/" in u for u in urls) and not messagebox.askyesno(
                APP_TITLE, "Downloading an artist downloads ALL of their albums and singles. Continue?"):
            return
        self._start_download(urls)

    def _download_links(self):
        items = [ln.strip() for ln in self.links.get("1.0", "end").splitlines() if ln.strip()]
        if not items:
            messagebox.showinfo(APP_TITLE, "Paste at least one TIDAL link first.")
            return
        self._start_download(items)

    def _start_download(self, items):
        self._save_settings()
        if not self.session.is_pkce and QUALITIES[self.settings["quality"]] in (
                Quality.high_lossless, Quality.hi_res_lossless):
            print("Note: you used Quick sign-in, so TIDAL will send 320 kbps AAC. "
                  "Use 'Lossless sign-in' for FLAC.")
        self.busy = True
        self.cancel.clear()
        self._refresh_buttons()
        threading.Thread(target=self._download_worker, args=(items,), daemon=True).start()

    def _progress(self, frac, label):
        def apply():
            if label is not None:
                self.work_var.set(label)
            if frac is None:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=frac)
        self.after(0, apply)

    def _download_worker(self, items):
        dl = Downloader(self.session, dict(self.settings), self._progress, self.cancel)
        for n, item in enumerate(items, 1):
            if self.cancel.is_set():
                break
            print(f"\n=== [{n}/{len(items)}] {item}")
            try:
                dl.run(item)
            except Exception as e:
                dl.count("failed")
                print(f"FAILED: {e}")
                print(traceback.format_exc(limit=2))
        self._save_session()
        dl.close()
        print(f"\n=== Finished: {dl.ok} downloaded, {dl.skipped} skipped, {dl.failed} failed.")
        self._ui(self._download_done)

    def _download_done(self):
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.work_var.set("Cancelled" if self.cancel.is_set() else "Done")
        self._refresh_buttons()

    def _cancel_download(self):
        self.cancel.set()
        self.work_var.set("Cancelling...")

    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP_TITLE, "A download is running. Quit anyway?"):
            return
        self.cancel.set()
        self.player.stop()
        self.destroy()


def _single_instance():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47391))
    except OSError:
        return None
    return s


def _existing_window():
    """(hwnd, pid) of the already-running app window, or (None, None)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, APP_TITLE) or user32.FindWindowW(None, "tidal-dl GUI")  # old name
    if not hwnd:
        return None, None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def _bring_to_front(hwnd):
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE (un-minimise)
    user32.SetForegroundWindow(hwnd)


def main():
    lock = _single_instance()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        hwnd, pid = _existing_window()
        restart = messagebox.askyesno(
            APP_TITLE,
            "TIDALOID is already open.\n\n"
            "Yes = close the open copy and start a fresh one (any running download stops)\n"
            "No = show the open window")
        if not restart:
            if hwnd:
                _bring_to_front(hwnd)
            root.destroy()
            return
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=0x08000000)
        for _ in range(20):             # wait for the old copy to release its lock
            time.sleep(0.25)
            lock = _single_instance()
            if lock:
                break
        root.destroy()
        if lock is None:
            messagebox.showerror(APP_TITLE, "Could not close the open copy. Close it from the taskbar.")
            return
    App().mainloop()


if __name__ == "__main__":
    main()
