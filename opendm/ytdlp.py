"""YouTube (and similar sites) through yt-dlp.

YouTube doesn't serve plain files: it hands out scrambled, short-lived links that change
whenever YouTube updates its player. Instead of chasing that ourselves, OpenDM drives
yt-dlp (https://github.com/yt-dlp/yt-dlp), a widely used open-source tool that keeps up
with those changes. OpenDM.exe ships with yt-dlp (and ffmpeg) and keeps yt-dlp updated;
when running from source install them yourself:  winget install yt-dlp ffmpeg

OpenDM shows yt-dlp's progress in its own window, and pausing stops yt-dlp, which resumes
from its partial files next time. DRM-protected and live videos are refused.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

from .engine import Download, Status
from .hls import find_ffmpeg
from . import bundle
from .util import category_for, sanitize_filename, unique_path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SITES = re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I)
MARK = "OPENDM"


class YtdlpMissing(Exception):
    pass


def is_ytdlp_url(url: str) -> bool:
    try:
        return bool(SITES.search(urllib.parse.urlsplit(url).hostname or ""))
    except ValueError:
        return False


_update_lock = threading.Lock()


def managed_ytdlp() -> str | None:
    """OpenDM's own copy of yt-dlp (unpacked from OpenDM.exe into the data folder)."""
    return bundle.install("yt-dlp")


def update_managed_ytdlp(max_age: float = 86400) -> str | None:
    """Let OpenDM's own yt-dlp update itself (at most once a day). Returns the new version.

    YouTube changes often and old yt-dlp versions stop working, so this matters.
    Only OpenDM's own copy is touched, never one the user installed.
    """
    exe = managed_ytdlp()
    if not bundle.is_managed(exe):
        return None
    state = bundle.read_state("yt-dlp")
    if time.time() - state.get("checked", 0) < max_age:
        return None
    with _update_lock:
        try:
            subprocess.run([exe, "-U"], capture_output=True, timeout=180, creationflags=NO_WINDOW, env=_env())
            out = subprocess.run([exe, "--version"], capture_output=True, timeout=60,
                                 creationflags=NO_WINDOW, env=_env())
            version = out.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            return None
        bundle.write_state("yt-dlp", {"version": version or state.get("version", ""), "checked": time.time()})
        return version


def find_ytdlp(configured: str | None = None) -> str | None:
    if configured and os.path.isfile(configured):
        return configured
    managed = managed_ytdlp()  # the copy that ships with OpenDM.exe, kept up to date
    if managed:
        return managed
    found = shutil.which("yt-dlp")
    if found:
        return found
    if sys.platform == "win32":  # winget installs often aren't on PATH for GUI apps yet
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet")
        for sub in ("Links", "Packages"):
            root_dir = os.path.join(base, sub)
            if os.path.isdir(root_dir):
                for root, _dirs, files in os.walk(root_dir):
                    if "yt-dlp.exe" in files:
                        return os.path.join(root, "yt-dlp.exe")
    return None


def _command(exe: str) -> list[str]:
    # A .py path lets the tests use a fake yt-dlp.
    return [sys.executable, exe] if exe.endswith(".py") else [exe]


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _last_error(text: str) -> str:
    errors = [l for l in text.splitlines() if l.startswith("ERROR:")]
    msg = errors[-1][6:].strip() if errors else (text.strip().splitlines() or ["yt-dlp failed"])[-1]
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)
    low = msg.lower()
    if "drm" in low:
        return "This video is DRM-protected, so it can't be downloaded."
    if "does not pass filter" in low or "is live" in low or "live event" in low:
        return "This is a live stream. OpenDM can only download videos that have an end."
    if "sign in" in low or "not a bot" in low:
        return "YouTube wants a sign-in for this video (age-restricted or bot check). Try again later."
    if "private video" in low or "unavailable" in low:
        return "This video is private or unavailable."
    if "unsupported url" in low:
        return "yt-dlp doesn't support this address."
    return msg[:300] + " (if YouTube changed something, update yt-dlp: winget upgrade yt-dlp)"


_probe_cache: dict[str, tuple[float, dict]] = {}


def probe(url: str, exe: str | None, can_merge: bool = True) -> dict:
    """Ask yt-dlp which qualities a video has. Returns {title, duration, options: [...]}.

    Without ffmpeg (can_merge=False) only qualities that come as one file with sound are
    offered; on YouTube that's usually 360p. Higher ones need ffmpeg to join video and audio.
    """
    if not exe:
        raise YtdlpMissing("yt-dlp isn't installed. Install it with: winget install yt-dlp")
    key = (url, can_merge)
    hit = _probe_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    proc = subprocess.run(
        _command(exe) + ["-J", "--no-playlist", "--no-warnings", "--", url],
        capture_output=True, timeout=60, creationflags=NO_WINDOW, env=_env(),
    )
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 or not out.strip():
        raise RuntimeError(_last_error(proc.stderr.decode("utf-8", "replace")))
    info = json.loads(out)
    if info.get("is_live"):
        raise RuntimeError("This is a live stream. OpenDM can only download videos that have an end.")
    formats = info.get("formats") or []
    audio_size = max((f.get("filesize") or f.get("filesize_approx") or 0 for f in formats
                      if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")), default=0)
    best: dict[int, dict] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none") or f.get("has_drm"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if f.get("acodec") in (None, "none"):
            if not can_merge:
                continue
            size = size + audio_size if size else 0
        cur = best.get(h)
        if cur is None or size > cur["size"] or (f.get("fps") or 0) > cur["fps"]:
            best[h] = {"height": h, "size": size, "fps": f.get("fps") or 0}
    result = {
        "title": info.get("title") or "video",
        "duration": info.get("duration") or 0,
        "options": sorted(best.values(), key=lambda o: -o["height"]),
        "audio_size": audio_size,
        "needs_ffmpeg": not can_merge,
    }
    _probe_cache[key] = (time.time(), result)
    return result


def format_for(quality, can_merge: bool = True) -> str:
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio"
    try:
        h = int(quality or 0)
    except (TypeError, ValueError):
        h = 0
    if not can_merge:  # no ffmpeg: only formats that already have video and sound together
        return f"b[height<={h}]/b" if h else "b"
    if h:
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    return "bv*+ba/b"


class YtdlpDownload(Download):
    kind = "ytdlp"

    def __init__(self, url, folder, filename=None, connections=8, headers=None, id=None, quality=None):
        super().__init__(url, folder, filename, connections, headers, id)
        self.quality = quality          # height (e.g. 720), "audio", or None for best
        self.ytdlp_path: str | None = None
        self.ffmpeg_path: str | None = None
        self.stage = 0                  # yt-dlp downloads video and audio one after another
        self._stage_fmt = None
        self._format = format_for(quality)
        self._done_bytes = 0            # bytes of finished stages
        self._cur = 0
        self._cur_total = 0
        self._proc: subprocess.Popen | None = None
        self.note: str | None = None
        self.resumable = True

    @property
    def size_estimated(self) -> bool:
        return self.status != Status.COMPLETE

    @property
    def downloaded(self) -> int:
        if self.status == Status.COMPLETE and self.total is not None:
            return self.total
        return self._done_bytes + self._cur

    @property
    def progress(self) -> float | None:
        if self.status == Status.COMPLETE:
            return 1.0
        if not self.total:
            return None
        return min(0.999, self.downloaded / self.total)

    @property
    def active_connections(self) -> int:
        return self.connections if self.status == Status.DOWNLOADING else 0

    def bar(self):
        return self.total, ([(0, self.total - 1, self.downloaded)] if self.total else [])

    def refresh_from_disk(self) -> None:
        pass  # yt-dlp's .part files hold the progress; it picks them up itself

    def label(self) -> str:
        if self.quality == "audio":
            return "audio only"
        return f"{self.quality}p" if self.quality else "best quality"

    # ------------------------------------------------------------------ control
    def pause(self, wait: bool = False) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        super().pause(wait)

    def _stem(self) -> str:
        stem = os.path.splitext(self.filename or "")[0] if self.filename else "%(title)s"
        if self.filename and self.filename.lower().endswith((".mp4", ".m4a", ".webm", ".mkv")):
            stem = os.path.splitext(self.filename)[0]
        return stem

    def _args(self, exe: str) -> list[str]:
        ff = find_ffmpeg(self.ffmpeg_path)
        self._format = format_for(self.quality, can_merge=bool(ff))
        out_tmpl = os.path.join(self.temp_dir, "%(title).150B [%(id)s].%(ext)s")
        progress = (f"download:{MARK} %(info.format_id)s %(progress.downloaded_bytes)s "
                    "%(progress.total_bytes)s %(progress.total_bytes_estimate)s")
        args = _command(exe) + [
            "--no-playlist", "--newline", "--no-colors", "--continue", "--no-mtime",
            "--progress", "--progress-template", progress,
            "--print", f"after_move:{MARK}FILE %(filepath)s",
            "--print", f"before_dl:{MARK}TITLE %(title)s",
            "--no-simulate", "--match-filter", "!is_live",
            "-N", str(max(1, min(16, self.connections))),
            "-f", self._format,
            # Same resolution, but prefer H.264 + AAC: they play everywhere (TVs, editors,
            # the Windows player). Above 1080p YouTube only has VP9/AV1, which is used then.
            "-S", "res,vcodec:h264,acodec:aac",
            "-o", out_tmpl,
        ]
        if ff and self.quality != "audio":  # audio keeps the original m4a, no re-encoding
            args += ["--merge-output-format", "mp4"]
        if ff:
            args += ["--ffmpeg-location", ff]
        if self.limiter and self.limiter.rate > 0:
            args += ["-r", str(self.limiter.rate)]
        return args + ["--", self.url]

    def _handle_line(self, line: str, state: dict) -> None:
        if line.startswith(MARK + "FILE "):
            state["file"] = line[len(MARK) + 5 :].strip()
        elif line.startswith(MARK + "TITLE "):
            if not self.user_filename:
                self.filename = sanitize_filename(line[len(MARK) + 6 :].strip())
        elif line.startswith(MARK + " "):
            parts = line.split()
            if len(parts) < 5:
                return
            fmt = parts[1]
            num = [int(float(p)) if p not in ("NA", "None") else 0 for p in parts[2:5]]
            got, total, est = num
            with self._lock:
                if self._stage_fmt is not None and fmt != self._stage_fmt:
                    self._done_bytes += max(self._cur, self._cur_total)
                    self._cur = 0
                    self.stage += 1
                self._stage_fmt = fmt
                self._cur = got
                self._cur_total = total or est
                # Estimate: finished stages + this stage (+ audio still to come for stage 0).
                t = self._done_bytes + self._cur_total
                if self.stage == 0 and "+" in self._format and self.quality != "audio":
                    t = int(t * 1.08)  # audio is usually a few % of a video
                self.total = max(t, self.downloaded)
            if self.status == Status.CONNECTING:
                self.status = Status.DOWNLOADING
        elif line.startswith("[Merger]") or line.startswith("[ExtractAudio]") or line.startswith("[VideoConvertor]"):
            self.status = Status.ASSEMBLING

    def _run(self) -> None:
        try:
            exe = find_ytdlp(self.ytdlp_path)
            if not exe:
                raise YtdlpMissing("yt-dlp isn't installed. Install it with:  winget install yt-dlp")
            if self.quality != "audio" and is_ytdlp_url(self.url) and not find_ffmpeg(self.ffmpeg_path):
                # YouTube only serves video and sound as separate streams; ffmpeg joins them.
                raise RuntimeError("YouTube video needs ffmpeg, which wasn't found. Install it with:  "
                                   "winget install ffmpeg  (or choose Audio only)")
            os.makedirs(self.temp_dir, exist_ok=True)
            self._done_bytes = self._cur = self._cur_total = 0
            self.stage = 0
            self._stage_fmt = None
            state: dict = {}
            proc = subprocess.Popen(
                self._args(exe), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=NO_WINDOW, env=_env(), cwd=self.temp_dir,
            )
            self._proc = proc
            err_lines: list[str] = []
            t = threading.Thread(target=lambda: err_lines.extend(
                proc.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True)
            t.start()
            for raw in proc.stdout:
                self._handle_line(raw.decode("utf-8", "replace").rstrip("\r\n"), state)
            proc.wait()
            t.join(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
            self._proc = None

            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            if proc.returncode != 0 or not state.get("file") or not os.path.exists(state["file"]):
                raise RuntimeError(_last_error("\n".join(err_lines)))
            self._finish(state["file"])
            self.status = Status.COMPLETE
            self.completed_at = time.time()
        except YtdlpMissing as e:
            self.status = Status.ERROR
            self.error = str(e)
        except BaseException as e:  # noqa: BLE001
            self.status = Status.ERROR
            self.error = str(e) or e.__class__.__name__
        finally:
            self.speed = 0.0
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:
                    pass

    def _finish(self, produced: str) -> None:
        self.status = Status.ASSEMBLING
        ext = os.path.splitext(produced)[1]
        stem = self._stem()
        if stem == "%(title)s":
            stem = re.sub(r"\s*\[[\w-]{6,}\]$", "", os.path.splitext(os.path.basename(produced))[0])
        if self.quality and self.quality != "audio":
            stem = f"{stem} ({self.quality}p)"
        folder = self.folder
        if self.auto_category:
            folder = os.path.join(folder, category_for("x" + ext))
        os.makedirs(folder, exist_ok=True)
        target = unique_path(os.path.join(folder, sanitize_filename(stem + ext)))
        shutil.move(produced, target)
        self.final_path = target
        self.filename = os.path.basename(target)
        self.total = os.path.getsize(target)
        self.delete_temp()

    # -------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(kind=self.kind, quality=self.quality, note=self.note, segments=[])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "YtdlpDownload":
        dl = cls(d["url"], d["folder"], None, d.get("connections", 8), d.get("headers"), d["id"], d.get("quality"))
        for k in ("filename", "user_filename", "total", "status", "error", "created", "completed_at",
                  "final_path", "auto_category", "note"):
            if k in d:
                setattr(dl, k, d[k])
        return dl
