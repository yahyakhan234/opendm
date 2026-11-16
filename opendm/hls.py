"""HLS (.m3u8) video stream downloader.

Most video sites don't serve one big .mp4. They serve a playlist that lists hundreds of
small chunks, often in several qualities and sometimes with the audio as a separate
playlist. This module:

1. reads the master playlist and picks a quality (best by default),
2. reads the media playlist(s) for video and, if separate, audio,
3. downloads the chunks over several connections (resumable: finished chunks are kept),
4. decrypts AES-128 chunks (plain HLS encryption with an openly served key),
5. joins the chunks and, if ffmpeg is installed, remuxes / merges them into one .mp4.

DRM-protected streams (SAMPLE-AES, Widevine, FairPlay, PlayReady) are refused, not
circumvented. Live streams are refused too (they never end).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse

from .aes import decrypt_cbc
from .engine import CHUNK, MAX_RETRIES, NETWORK_ERRORS, Download, Status, friendly_error
from . import bundle
from .util import category_for, filename_from_url, sanitize_filename, unique_path


class StreamError(Exception):
    """A problem the user should read as-is (DRM, live stream, bad playlist)."""


# ------------------------------------------------------------------ playlist parsing
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(s: str) -> dict:
    return {k: v.strip('"') for k, v in _ATTR.findall(s)}


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _drm_reason(attrs: dict) -> str | None:
    method = attrs.get("METHOD", "NONE").upper()
    fmt = attrs.get("KEYFORMAT", "identity").lower()
    if method in ("NONE", "AES-128") and fmt == "identity":
        return None
    if method == "NONE":
        return None
    return "This video is DRM-protected, so it can't be downloaded."


def parse_master(text: str, base: str) -> dict:
    variants, audio = [], {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-SESSION-KEY:"):
            reason = _drm_reason(parse_attrs(line.split(":", 1)[1]))
            if reason:
                raise StreamError(reason)
        elif line.startswith("#EXT-X-MEDIA:"):
            a = parse_attrs(line.split(":", 1)[1])
            if a.get("TYPE") == "AUDIO":
                audio.setdefault(a.get("GROUP-ID", ""), []).append(
                    {
                        "uri": urllib.parse.urljoin(base, a["URI"]) if a.get("URI") else None,
                        "name": a.get("NAME", ""),
                        "lang": a.get("LANGUAGE", ""),
                        "default": a.get("DEFAULT", "NO").upper() == "YES",
                    }
                )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            a = parse_attrs(line.split(":", 1)[1])
            uri = next((l for l in lines[i + 1 :] if not l.startswith("#")), None)
            if not uri:
                continue
            w = h = 0
            if "x" in a.get("RESOLUTION", ""):
                w, h = (int(x) for x in a["RESOLUTION"].lower().split("x")[:2])
            variants.append(
                {
                    "uri": urllib.parse.urljoin(base, uri),
                    "bandwidth": int(a.get("AVERAGE-BANDWIDTH") or a.get("BANDWIDTH") or 0),
                    "width": w,
                    "height": h,
                    "audio": a.get("AUDIO"),
                    "codecs": a.get("CODECS", ""),
                }
            )
    # Drop audio-only / I-frame-ish variants when real video variants exist.
    with_video = [v for v in variants if v["height"] or "avc" in v["codecs"] or "hvc" in v["codecs"]]
    return {"variants": with_video or variants, "audio": audio}


def choose_variant(master: dict, height: int | None = None) -> dict:
    vs = master["variants"]
    if not vs:
        raise StreamError("The playlist doesn't list any video streams.")
    if height:
        fitting = [v for v in vs if v["height"] and v["height"] <= height]
        if fitting:
            return max(fitting, key=lambda v: (v["height"], v["bandwidth"]))
    return max(vs, key=lambda v: (v["height"], v["bandwidth"]))


def choose_audio(master: dict, variant: dict) -> str | None:
    group = master["audio"].get(variant.get("audio") or "", [])
    with_uri = [a for a in group if a["uri"]]
    if not with_uri:
        return None  # audio is inside the video chunks
    return next((a["uri"] for a in with_uri if a["default"]), with_uri[0]["uri"])


def parse_media(text: str, base: str) -> dict:
    """Return {segments: [...], maps: [...], ended: bool}."""
    if not text.lstrip().startswith("#EXTM3U"):
        raise StreamError("That address isn't an HLS playlist.")
    segs, maps = [], []
    key = None
    cur_map = None
    seq = 0
    next_off = 0
    pending_range = None
    ended = "#EXT-X-ENDLIST" in text or re.search(r"#EXT-X-PLAYLIST-TYPE:\s*VOD", text) is not None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            a = parse_attrs(line.split(":", 1)[1])
            reason = _drm_reason(a)
            if reason:
                raise StreamError(reason)
            if a.get("METHOD", "NONE").upper() == "NONE":
                key = None
            else:
                iv = a.get("IV")
                key = {
                    "uri": urllib.parse.urljoin(base, a["URI"]),
                    "iv": bytes.fromhex(iv[2:] if iv.lower().startswith("0x") else iv).rjust(16, b"\0") if iv else None,
                }
        elif line.startswith("#EXT-X-MAP:"):
            a = parse_attrs(line.split(":", 1)[1])
            rng = None
            if a.get("BYTERANGE"):
                n, _, off = a["BYTERANGE"].partition("@")
                rng = (int(n), int(off or 0))
            maps.append({"uri": urllib.parse.urljoin(base, a["URI"]), "range": rng})
            cur_map = len(maps) - 1
        elif line.startswith("#EXT-X-BYTERANGE:"):
            n, _, off = line.split(":", 1)[1].partition("@")
            pending_range = (int(n), int(off) if off else next_off)
        elif not line.startswith("#"):
            rng = pending_range
            if rng:
                next_off = rng[0] + rng[1]
            pending_range = None
            segs.append(
                {
                    "uri": urllib.parse.urljoin(base, line),
                    "seq": seq,
                    "key": key,
                    "map": cur_map,
                    "range": rng,
                }
            )
            seq += 1
    if not ended:
        raise StreamError("This is a live stream. OpenDM can only download videos that have an end.")
    if not segs:
        raise StreamError("The playlist has no video chunks.")
    return {"segments": segs, "maps": maps}


# ----------------------------------------------------------------------- ffmpeg
def find_ffmpeg(configured: str | None = None) -> str | None:
    if configured and os.path.isfile(configured):
        return configured
    managed = bundle.install("ffmpeg")  # the copy that ships with OpenDM.exe
    if managed:
        return managed
    found = shutil.which("ffmpeg")
    if found:
        return found
    if sys.platform == "win32":  # winget installs often aren't on PATH for GUI apps yet
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")
    return None


# ----------------------------------------------------------------------- download
class HlsDownload(Download):
    kind = "hls"

    def __init__(self, url, folder, filename=None, connections=8, headers=None, id=None,
                 audio_url=None, quality=None):
        super().__init__(url, folder, filename, connections, headers, id)
        self.audio_url = audio_url    # separate audio playlist (if the site splits them)
        self.quality = quality        # preferred video height, None = best
        self.variant_label = ""       # e.g. "1080p"
        self.media_urls: list[str] = []
        self.tracks: list[dict] = []  # [{segments, maps}] per media playlist
        self.units = 0                # total chunks across tracks
        self.done_units = 0
        self.bytes_done = 0
        self._inflight = 0
        self._active = 0
        self.note: str | None = None
        self.ffmpeg_path: str | None = None
        self.resumable = True

    # ------------------------------------------------------------- info for UI
    @property
    def downloaded(self) -> int:
        if self.status == Status.COMPLETE and self.total is not None:
            return self.total
        return self.bytes_done + self._inflight

    @property
    def progress(self) -> float | None:
        if self.status == Status.COMPLETE:
            return 1.0
        if not self.units:
            return None
        return min(1.0, self.done_units / self.units)

    @property
    def size_estimated(self) -> bool:
        return self.status != Status.COMPLETE

    @property
    def active_connections(self) -> int:
        return self._active

    def bar(self):
        """(units, [(start, end, pos)]) - finished chunks drawn as filled runs."""
        if not self.units:
            return None, []
        done = self._done_flags()
        runs, start = [], None
        for i, d in enumerate(done + [False]):
            if d and start is None:
                start = i
            elif not d and start is not None:
                runs.append((start, i - 1, i))
                start = None
        return self.units, runs

    def _done_flags(self) -> list[bool]:
        flags = []
        for t, track in enumerate(self.tracks):
            for i in range(len(track["segments"])):
                flags.append(os.path.exists(self._chunk_path(t, i)))
        return flags

    # --------------------------------------------------------------- helpers
    def _chunk_path(self, t: int, i: int) -> str:
        return os.path.join(self.temp_dir, f"t{t}_{i:06d}.seg")

    def _map_path(self, t: int, m: int) -> str:
        return os.path.join(self.temp_dir, f"t{t}_map{m}.seg")

    def _get(self, url: str, rng: tuple | None = None) -> bytes:
        header = f"bytes={rng[1]}-{rng[1] + rng[0] - 1}" if rng else None
        with self._request(url, header) as r:
            return r.read()

    def _get_text(self, url: str) -> str:
        return self._get(url).decode("utf-8", "replace")

    def refresh_from_disk(self) -> None:
        if not os.path.isdir(self.temp_dir):
            return
        sizes = [os.path.getsize(os.path.join(self.temp_dir, f))
                 for f in os.listdir(self.temp_dir) if f.endswith(".seg") and "_map" not in f]
        self.done_units = len(sizes)
        self.bytes_done = sum(sizes)
        self._estimate_total()

    def _estimate_total(self) -> None:
        if self.done_units and self.units:
            self.total = int(self.bytes_done / self.done_units * self.units)

    # --------------------------------------------------------------- resolve
    def _resolve(self) -> None:
        text = self._get_text(self.url)
        media = [self.url]
        if is_master(text):
            master = parse_master(text, self.url)
            v = choose_variant(master, self.quality)
            self.variant_label = f"{v['height']}p" if v["height"] else ""
            media = [v["uri"]]
            audio = self.audio_url or choose_audio(master, v)
            if audio:
                media.append(audio)
            text = None
        elif self.audio_url:
            media.append(self.audio_url)

        tracks = []
        for i, url in enumerate(media):
            body = text if (i == 0 and text is not None) else self._get_text(url)
            if is_master(body):
                raise StreamError("Nested master playlists aren't supported.")
            tracks.append(parse_media(body, url))

        units = sum(len(t["segments"]) for t in tracks)
        if self.media_urls and (self.media_urls != media or self.units != units):
            self.delete_temp()  # stream changed since we paused: start over
            self.bytes_done = self.done_units = 0
        self.media_urls = media
        self.tracks = tracks
        self.units = units
        if not self.filename:
            stem = filename_from_url(self.url)
            stem = re.sub(r"\.m3u8?$", "", stem, flags=re.I)
            if stem.lower() in ("index", "master", "playlist", "prog_index", "manifest", ""):
                stem = "video"
            self.filename = sanitize_filename(stem)

    def _fetch_maps(self) -> None:
        for t, track in enumerate(self.tracks):
            for m, mp in enumerate(track["maps"]):
                path = self._map_path(t, m)
                if not os.path.exists(path):
                    data = self._get(mp["uri"], mp["range"])
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)

    # --------------------------------------------------------------- workers
    def _fetch_chunk(self, t: int, i: int, keys: dict) -> None:
        seg = self.tracks[t]["segments"][i]
        rng = seg["range"]
        header = f"bytes={rng[1]}-{rng[1] + rng[0] - 1}" if rng else None
        buf = bytearray()
        try:
            with self._request(seg["uri"], header) as r:
                while True:
                    if self._stop.is_set():
                        return
                    data = r.read(CHUNK)
                    if not data:
                        break
                    if self.limiter:
                        self.limiter.consume(len(data), self._stop)
                    buf += data
                    with self._lock:
                        self._inflight += len(data)
        finally:
            with self._lock:
                self._inflight -= len(buf)
        data = bytes(buf)
        if seg["key"]:
            k = seg["key"]
            with self._lock:
                key = keys.get(k["uri"])
            if key is None:
                key = self._get(k["uri"])
                if len(key) != 16:
                    raise StreamError("The stream's decryption key is invalid (it may be DRM-protected).")
                with self._lock:
                    keys[k["uri"]] = key
            iv = k["iv"] or seg["seq"].to_bytes(16, "big")
            data = decrypt_cbc(key, iv, data)
        path = self._chunk_path(t, i)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        with self._lock:
            self.done_units += 1
            self.bytes_done += len(data)
            self._estimate_total()

    def _hls_worker(self, todo: list, keys: dict) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not todo:
                        return
                    t, i = todo.pop(0)
                    self._active += 1
                try:
                    attempts = 0
                    while not self._stop.is_set():
                        try:
                            self._fetch_chunk(t, i, keys)
                            break
                        except urllib.error.HTTPError as e:
                            if e.code in (401, 403, 404, 410):
                                raise
                            attempts += 1
                            if attempts > MAX_RETRIES:
                                raise
                            self._stop.wait(min(30, 2 ** attempts))
                        except NETWORK_ERRORS:
                            attempts += 1
                            if attempts > MAX_RETRIES:
                                raise
                            self._stop.wait(min(30, 2 ** attempts))
                finally:
                    with self._lock:
                        self._active -= 1
        except BaseException as e:  # noqa: BLE001
            self._fatal = e
            self._stop.set()

    # -------------------------------------------------------------- assemble
    def _join_track(self, t: int, out_path: str) -> None:
        track = self.tracks[t]
        last_map = None
        with open(out_path, "wb") as out:
            for i, seg in enumerate(track["segments"]):
                if seg["map"] is not None and seg["map"] != last_map:
                    with open(self._map_path(t, seg["map"]), "rb") as f:
                        shutil.copyfileobj(f, out)
                    last_map = seg["map"]
                with open(self._chunk_path(t, i), "rb") as f:
                    shutil.copyfileobj(f, out, 4 * 1024 * 1024)

    def _assemble(self) -> None:
        self.status = Status.ASSEMBLING
        joined = []
        for t, track in enumerate(self.tracks):
            ext = ".mp4" if track["maps"] else ".ts"
            path = os.path.join(self.temp_dir, f"track{t}{ext}")
            self._join_track(t, path)
            joined.append(path)

        folder = self.folder
        stem = os.path.splitext(self.filename or "video")[0]
        if self.variant_label and self.variant_label not in stem:
            stem = f"{stem} ({self.variant_label})"
        if self.auto_category:
            folder = os.path.join(folder, category_for(stem + ".mp4"))
        os.makedirs(folder, exist_ok=True)

        ffmpeg = find_ffmpeg(self.ffmpeg_path)
        if ffmpeg:
            target = unique_path(os.path.join(folder, stem + ".mp4"))
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            for p in joined:
                cmd += ["-i", p]
            cmd += ["-map", "0:v?", "-map", "0:a?"]
            if len(joined) > 1:
                cmd += ["-map", "1:a?"]
            cmd += ["-c", "copy", "-movflags", "+faststart", target]
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            proc = subprocess.run(cmd, capture_output=True, creationflags=flags)
            if proc.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0:
                self._finish(target)
                return
            try:
                os.remove(target)
            except OSError:
                pass
            self.note = "ffmpeg couldn't convert this stream, so it was saved in its original format."

        # No ffmpeg (or it failed): keep the joined stream as-is. .ts plays in VLC,
        # MPC-HC and the Windows Media Player app.
        main = joined[0]
        target = unique_path(os.path.join(folder, stem + os.path.splitext(main)[1]))
        shutil.move(main, target)
        if len(joined) > 1:
            audio_target = unique_path(os.path.join(folder, f"{stem} (audio){os.path.splitext(joined[1])[1]}"))
            shutil.move(joined[1], audio_target)
            self.note = (self.note or "") + " Video and audio were saved as two files; install ffmpeg to merge them."
        elif not ffmpeg and main.endswith(".ts"):
            self.note = "Saved as .ts. Install ffmpeg to get .mp4 files."
        self._finish(target)

    def _finish(self, target: str) -> None:
        self.final_path = target
        self.filename = os.path.basename(target)
        self.total = os.path.getsize(target)
        self.delete_temp()

    # ------------------------------------------------------------------- run
    def _run(self) -> None:
        try:
            self._resolve()
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            os.makedirs(self.temp_dir, exist_ok=True)
            for f in os.listdir(self.temp_dir):
                if f.endswith(".tmp"):
                    os.remove(os.path.join(self.temp_dir, f))
            self.refresh_from_disk()
            self._fetch_maps()
            self.status = Status.DOWNLOADING

            todo = [(t, i) for t, tr in enumerate(self.tracks) for i in range(len(tr["segments"]))
                    if not os.path.exists(self._chunk_path(t, i))]
            keys: dict = {}
            n = max(1, min(self.connections, len(todo)))
            workers = [threading.Thread(target=self._hls_worker, args=(todo, keys), daemon=True)
                       for _ in range(n)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

            if self._fatal is not None:
                raise self._fatal
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            if self.done_units < self.units:
                raise IOError("Some video chunks are missing - try resuming")
            self._assemble()
            self.status = Status.COMPLETE
            self.completed_at = time.time()
        except StreamError as e:
            self.status = Status.ERROR
            self.error = str(e)
        except urllib.error.HTTPError as e:
            self.status = Status.ERROR
            self.error = f"HTTP {e.code} {e.reason}"
            if e.code in (401, 403, 410):
                self.error += " - the video link may have expired; start it again from the page"
        except BaseException as e:  # noqa: BLE001
            self.status = Status.ERROR
            self.error = friendly_error(e)
        finally:
            self.speed = 0.0 if self.status != Status.DOWNLOADING else self.speed
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:
                    pass

    # ----------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            kind=self.kind,
            audio_url=self.audio_url,
            quality=self.quality,
            variant_label=self.variant_label,
            media_urls=self.media_urls,
            units=self.units,
            note=self.note,
            segments=[],
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HlsDownload":
        dl = cls(d["url"], d["folder"], None, d.get("connections", 8), d.get("headers"), d["id"],
                 d.get("audio_url"), d.get("quality"))
        for k in ("filename", "user_filename", "total", "status", "error", "created", "completed_at",
                  "final_path", "auto_category", "variant_label", "media_urls", "units", "note"):
            if k in d:
                setattr(dl, k, d[k])
        dl.status = dl.status or Status.PAUSED
        if dl.status == Status.COMPLETE:
            dl.done_units = dl.units
        return dl
