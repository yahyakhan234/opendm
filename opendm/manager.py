from __future__ import annotations

import json
import os
import threading
import time

from .engine import Download, RateLimiter, Status
from .hls import HlsDownload
from .ytdlp import YtdlpDownload, is_ytdlp_url
from .util import category_for, data_dir, default_download_dir

DEFAULT_SETTINGS = {
    "download_dir": default_download_dir(),
    "connections": 8,
    "max_concurrent": 3,
    "speed_limit_kbps": 0,
    "clipboard_monitor": False,
    "confirm_browser_downloads": True,
    "port": 6801,
    "sort_into_categories": False,
    "ffmpeg_path": "",
    "ytdlp_path": "",
    "minimize_to_tray": True,
    "tray_hint_shown": False,
}

class DownloadManager:
    def __init__(self, root: str | None = None):
        self.root = root or data_dir()
        self.temp_root = os.path.join(self.root, "temp")
        os.makedirs(self.temp_root, exist_ok=True)
        self.state_file = os.path.join(self.root, "downloads.json")
        self.settings_file = os.path.join(self.root, "settings.json")
        self.settings = dict(DEFAULT_SETTINGS)
        self.downloads: list[Download] = []
        self.limiter = RateLimiter()
        self._lock = threading.RLock()
        self._dirty = False
        self._running = True
        self.listeners = []  # callables(event, download) - invoked from worker threads

        self._load()
        self.apply_settings()
        self._ticker = threading.Thread(target=self._tick, daemon=True, name="dm-ticker")
        self._ticker.start()
        self._schedule()

    # ---------------------------------------------------------- settings
    def apply_settings(self) -> None:
        self.limiter.rate = int(self.settings.get("speed_limit_kbps") or 0) * 1024
        for d in getattr(self, "downloads", []):
            self._tool_paths(d)
        self._save_settings()
        self._schedule()

    def _save_settings(self) -> None:
        _atomic_write_json(self.settings_file, self.settings)

    # ------------------------------------------------------------ public
    def add(self, url, folder=None, filename=None, connections=None, headers=None, start=True,
            kind=None, audio_url=None, quality=None) -> Download:
        auto_cat = folder is None and bool(self.settings.get("sort_into_categories"))
        folder = folder or self.settings["download_dir"]
        conns = connections or self.settings["connections"]
        if kind == "ytdlp" or (kind in (None, "file") and is_ytdlp_url(url)):
            dl = YtdlpDownload(url, folder, filename, conns, headers, quality=quality)
        elif kind == "hls" or (kind is None and is_hls_url(url)):
            dl = HlsDownload(url, folder, filename, conns, headers, audio_url=audio_url, quality=quality)
        else:
            dl = Download(url, folder, filename, conns, headers)
        dl.auto_category = auto_cat
        dl.status = Status.QUEUED if start else Status.PAUSED
        self._attach(dl)
        with self._lock:
            self.downloads.append(dl)
            self._dirty = True
        self._emit("added", dl)
        self._schedule()
        return dl

    def resume(self, dl: Download) -> None:
        if dl.status in (Status.PAUSED, Status.ERROR, Status.QUEUED):
            dl.status = Status.QUEUED
            dl.error = None
            self._dirty = True
            self._schedule()

    def pause(self, dl: Download) -> None:
        if dl.status in Status.ACTIVE or dl.status == Status.QUEUED:
            dl.pause()
            if dl.status == Status.QUEUED:
                dl.status = Status.PAUSED
            self._dirty = True

    def redownload(self, dl: Download) -> None:
        dl.pause(wait=True)
        dl.delete_temp()
        dl.segments = []
        dl.total = None
        dl.final_path = None
        dl.completed_at = None
        if isinstance(dl, HlsDownload):
            dl.media_urls, dl.units, dl.done_units, dl.bytes_done, dl.note = [], 0, 0, 0, None
        if not dl.user_filename:
            dl.filename = None
        dl.status = Status.QUEUED
        self._dirty = True
        self._schedule()

    def remove(self, dl: Download, delete_file: bool = False) -> None:
        dl.on_finish = None
        dl.pause(wait=True)
        dl.delete_temp()
        if delete_file and dl.final_path and os.path.exists(dl.final_path):
            try:
                os.remove(dl.final_path)
            except OSError:
                pass
        with self._lock:
            if dl in self.downloads:
                self.downloads.remove(dl)
            self._dirty = True
        self._emit("removed", dl)
        self._schedule()

    def pause_all(self) -> None:
        for dl in list(self.downloads):
            self.pause(dl)

    def resume_all(self) -> None:
        for dl in list(self.downloads):
            if dl.status in (Status.PAUSED, Status.ERROR):
                self.resume(dl)

    def total_speed(self) -> float:
        return sum(d.speed for d in self.downloads if d.status == Status.DOWNLOADING)

    def shutdown(self) -> None:
        self._running = False
        active = [d for d in self.downloads if d.is_running()]
        for d in active:
            d.on_finish = None
            d.pause()
        for d in active:
            d.pause(wait=True)
            d.status = Status.QUEUED  # resume these automatically next launch
        self.save()

    # ----------------------------------------------------------- internal
    def _tool_paths(self, dl: Download) -> None:
        if isinstance(dl, (HlsDownload, YtdlpDownload)):
            dl.ffmpeg_path = self.settings.get("ffmpeg_path") or None
        if isinstance(dl, YtdlpDownload):
            dl.ytdlp_path = self.settings.get("ytdlp_path") or None

    def _attach(self, dl: Download) -> None:
        dl.temp_root = self.temp_root
        self._tool_paths(dl)
        dl.limiter = self.limiter
        dl.on_finish = self._on_finish

    def _on_finish(self, dl: Download) -> None:
        self._dirty = True
        self._emit("finished", dl)
        self._schedule()

    def _emit(self, event: str, dl: Download) -> None:
        for fn in list(self.listeners):
            try:
                fn(event, dl)
            except Exception:
                pass

    def _schedule(self) -> None:
        if not self._running:
            return
        with self._lock:
            limit = max(1, int(self.settings.get("max_concurrent") or 1))
            running = sum(1 for d in self.downloads if d.is_running())
            for d in self.downloads:
                if running >= limit:
                    break
                if d.status == Status.QUEUED and not d.is_running():
                    d.start()
                    running += 1

    def _tick(self) -> None:
        last_save = time.monotonic()
        while self._running:
            time.sleep(0.5)
            for d in list(self.downloads):
                if d.status == Status.DOWNLOADING:
                    d.sample_speed()
                    self._dirty = True
            if self._dirty and time.monotonic() - last_save > 3:
                self.save()
                last_save = time.monotonic()

    def save(self) -> None:
        with self._lock:
            data = [d.to_dict() for d in self.downloads]
            self._dirty = False
        _atomic_write_json(self.state_file, data)

    def _load(self) -> None:
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            with open(self.state_file, encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        for item in items:
            try:
                cls = {"hls": HlsDownload, "ytdlp": YtdlpDownload}.get(item.get("kind"), Download)
                dl = cls.from_dict(item)
            except (KeyError, TypeError):
                continue
            self._attach(dl)
            if dl.status != Status.COMPLETE:
                dl.refresh_from_disk()
            self.downloads.append(dl)


def is_hls_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith((".m3u8", ".m3u"))


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass
