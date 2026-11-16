"""Download engine: multi-connection, resumable, dynamically segmented HTTP downloads.

How it works
------------
1. A probe request (``Range: bytes=0-0``) finds the file size, the final URL after
   redirects, the server-supplied filename, and whether byte ranges are supported.
2. The file is split into N segments. Each segment is downloaded by its own
   connection into its own temp file, so nothing ever has to zero-fill a huge
   preallocated file and resuming is just "append to the segment file".
3. When a connection finishes its segment it steals half of the biggest remaining
   segment (dynamic segmentation), so every connection stays busy until the end.
4. When every segment is complete, the pieces are assembled into the final file.
"""

from __future__ import annotations

import collections
import http.client
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

from .util import category_for, filename_from_disposition, filename_from_url, sanitize_filename, unique_path

CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024          # never split a segment into pieces smaller than this
MAX_RETRIES = 10
CONNECT_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    socket.timeout,
    ConnectionError,
    ssl.SSLError,
    TimeoutError,
)


class Status:
    QUEUED = "Queued"
    CONNECTING = "Connecting"
    DOWNLOADING = "Downloading"
    ASSEMBLING = "Assembling"
    PAUSED = "Paused"
    COMPLETE = "Complete"
    ERROR = "Error"

    ACTIVE = {CONNECTING, DOWNLOADING, ASSEMBLING}


def friendly_error(e: BaseException) -> str:
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return "Couldn't find the server - check the address and your internet connection"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "The server stopped responding (timed out)"
    if isinstance(reason, ssl.SSLError):
        return f"Secure connection failed ({getattr(reason, 'reason', None) or reason})"
    if isinstance(reason, ConnectionRefusedError):
        return "The server refused the connection"
    if isinstance(reason, ConnectionError):
        return "The connection was lost"
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "Not enough disk space"
    return str(reason) or e.__class__.__name__


class RateLimiter:
    """Token bucket shared by every connection of every download (bytes/second, 0 = unlimited)."""

    def __init__(self, bytes_per_sec: int = 0):
        self.rate = bytes_per_sec
        self._allowance = 0.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int, stop: threading.Event | None = None) -> None:
        rate = self.rate
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._allowance = min(rate, self._allowance + (now - self._last) * rate)
            self._last = now
            self._allowance -= n
            deficit = -self._allowance
        if deficit > 0:
            delay = deficit / rate
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)


class Segment:
    __slots__ = ("start", "end", "pos", "active", "done")

    def __init__(self, start: int, end: int | None, done: bool = False):
        self.start = start
        self.end = end            # inclusive; None = unknown length (stream until EOF)
        self.pos = start          # next byte to fetch
        self.active = False
        self.done = done

    @property
    def remaining(self) -> int:
        if self.end is None:
            return 0
        return max(0, self.end - self.pos + 1)

    @property
    def complete(self) -> bool:
        return self.done or (self.end is not None and self.pos > self.end)

    def to_dict(self):
        return {"start": self.start, "end": self.end, "done": self.done}


class Download:
    kind = "file"
    size_estimated = False

    def __init__(
        self,
        url: str,
        folder: str,
        filename: str | None = None,
        connections: int = 8,
        headers: dict | None = None,
        id: str | None = None,
    ):
        self.id = id or uuid.uuid4().hex
        self.url = url
        self.final_url = url
        self.folder = folder
        self.filename = sanitize_filename(filename) if filename else None
        self.user_filename = bool(filename)
        self.connections = max(1, min(32, int(connections)))
        self.headers = dict(headers or {})
        self.total: int | None = None
        self.resumable = False
        self.segments: list[Segment] = []
        self.status = Status.QUEUED
        self.error: str | None = None
        self.created = time.time()
        self.completed_at: float | None = None
        self.final_path: str | None = None
        self.auto_category = False  # put the finished file in a <Category> subfolder

        self.temp_root: str = ""  # set by the manager
        self.limiter: RateLimiter | None = None
        self.on_finish = None     # callback(download)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal: BaseException | None = None
        self._samples: collections.deque = collections.deque(maxlen=12)
        self.speed = 0.0

    # ------------------------------------------------------------------ info
    @property
    def temp_dir(self) -> str:
        return os.path.join(self.temp_root, self.id)

    @property
    def downloaded(self) -> int:
        if self.status == Status.COMPLETE and self.total is not None:
            return self.total
        with self._lock:
            return sum(s.pos - s.start for s in self.segments)

    @property
    def progress(self) -> float | None:
        if self.status == Status.COMPLETE:
            return 1.0
        if not self.total:
            return None
        return min(1.0, self.downloaded / self.total)

    @property
    def eta(self) -> float | None:
        if not self.total or self.speed <= 1:
            return None
        return (self.total - self.downloaded) / self.speed

    @property
    def active_connections(self) -> int:
        with self._lock:
            return sum(1 for s in self.segments if s.active)

    def display_name(self) -> str:
        return self.filename or filename_from_url(self.url)

    def segment_snapshot(self):
        """[(start, end, pos)] for drawing the segment bar."""
        with self._lock:
            return [(s.start, s.end, s.pos) for s in self.segments]

    def bar(self):
        """(total_units, [(start, end, pos)]) for drawing the per-connection bar."""
        return self.total, self.segment_snapshot()

    def refresh_from_disk(self) -> None:
        """Show correct progress for unfinished downloads before they are resumed."""
        with self._lock:
            for seg in self.segments:
                path = os.path.join(self.temp_dir, f"{seg.start}.part")
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if seg.end is not None:
                    size = min(size, seg.end - seg.start + 1)
                seg.pos = seg.start + size

    def sample_speed(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.downloaded))
        if len(self._samples) >= 2:
            t0, b0 = self._samples[0]
            t1, b1 = self._samples[-1]
            self.speed = (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    # ------------------------------------------------------------ lifecycle
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._fatal = None
        self.error = None
        self.status = Status.CONNECTING
        self._samples.clear()
        self._thread = threading.Thread(target=self._run, name=f"dl-{self.id[:6]}", daemon=True)
        self._thread.start()

    def pause(self, wait: bool = False) -> None:
        self._stop.set()
        if not self.is_running() and self.status == Status.QUEUED:
            self.status = Status.PAUSED
        if wait and self._thread is not None:
            self._thread.join(timeout=15)

    def delete_temp(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # --------------------------------------------------------------- worker
    def _request(self, url: str, range_header: str | None = None):
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
        headers.update({k: v for k, v in self.headers.items() if v})
        if range_header:
            headers["Range"] = range_header
        req = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)

    def _probe(self) -> None:
        """Find size, final URL, filename and range support."""
        try:
            r = self._request(self.url, "bytes=0-0")
        except urllib.error.HTTPError as e:
            if e.code != 416:
                raise
            r = self._request(self.url)  # some servers reject ranges on tiny/empty files
        with r:
            self.final_url = r.geturl() or self.url
            code = getattr(r, "status", None) or r.getcode() or 200
            content_range = r.headers.get("Content-Range", "")
            total = None
            resumable = False
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", content_range or "")
            if code == 206 and m:
                total = int(m.group(1))
                resumable = True
            else:
                cl = r.headers.get("Content-Length")
                total = int(cl) if cl and cl.isdigit() else None
            server_name = filename_from_disposition(r.headers.get("Content-Disposition"))

        if self.segments and (self.total != total or not resumable):
            # File changed on the server (or ranges no longer work): start over.
            self.delete_temp()
            self.segments = []

        self.total = total
        self.resumable = resumable and total is not None and total > 0
        if not self.user_filename:
            self.filename = sanitize_filename(server_name or filename_from_url(self.final_url))

    def _plan_segments(self) -> None:
        if self.segments:
            return
        if self.resumable and self.total:
            n = max(1, min(self.connections, self.total // MIN_SPLIT or 1))
            size = self.total // n
            self.segments = [
                Segment(i * size, (self.total - 1) if i == n - 1 else (i + 1) * size - 1) for i in range(n)
            ]
        elif self.total == 0:
            self.segments = [Segment(0, -1, done=True)]
        else:
            self.segments = [Segment(0, None)]

    def _seg_path(self, seg: Segment) -> str:
        return os.path.join(self.temp_dir, f"{seg.start}.part")

    def _sync_from_disk(self) -> None:
        """Resume: trust the real size of each segment file."""
        os.makedirs(self.temp_dir, exist_ok=True)
        for seg in self.segments:
            path = self._seg_path(seg)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if not self.resumable and not seg.done:
                size = 0  # can't resume a stream without ranges
                if os.path.exists(path):
                    os.remove(path)
            if seg.end is not None:
                cap = seg.end - seg.start + 1
                if size > cap:
                    with open(path, "r+b") as f:
                        f.truncate(cap)
                    size = cap
            seg.pos = seg.start + size
            seg.active = False

    def _next_segment(self) -> Segment | None:
        with self._lock:
            for seg in self.segments:
                if not seg.active and not seg.complete:
                    seg.active = True
                    return seg
            if not self.resumable:
                return None
            # Dynamic segmentation: split the largest in-flight segment in half.
            victim = max(
                (s for s in self.segments if s.active and not s.complete),
                key=lambda s: s.remaining,
                default=None,
            )
            if victim is None or victim.remaining < 2 * MIN_SPLIT:
                return None
            mid = victim.pos + victim.remaining // 2
            new = Segment(mid, victim.end)
            victim.end = mid - 1
            new.active = True
            self.segments.append(new)
            self.segments.sort(key=lambda s: s.start)
            return new

    def _fetch(self, seg: Segment) -> None:
        path = self._seg_path(seg)
        if self.resumable:
            with self._lock:
                seg.pos = seg.start + (os.path.getsize(path) if os.path.exists(path) else 0)
                if seg.complete:
                    return
                rng = f"bytes={seg.pos}-{seg.end}"
            mode = "ab"
        else:
            with self._lock:
                seg.pos = seg.start
            rng = None
            mode = "wb"

        with self._request(self.final_url, rng) as r:
            code = getattr(r, "status", None) or r.getcode() or 200
            if self.resumable and code != 206:
                raise IOError(f"Server ignored range request (HTTP {code})")
            with open(path, mode) as f:
                while not self._stop.is_set():
                    data = r.read(CHUNK)
                    if not data:
                        break
                    if self.limiter:
                        self.limiter.consume(len(data), self._stop)
                    with self._lock:
                        if seg.end is not None:
                            data = data[: max(0, seg.end - seg.pos + 1)]
                        seg.pos += len(data)
                        finished = seg.complete
                    f.write(data)
                    if finished:
                        return
                else:
                    return  # paused
        # Server closed the connection.
        if seg.end is None:
            with self._lock:
                seg.done = True
                seg.end = seg.pos - 1
                self.total = seg.pos
        elif not seg.complete:
            raise ConnectionError("Connection closed before segment finished")

    def _worker(self) -> None:
        try:
            while not self._stop.is_set():
                seg = self._next_segment()
                if seg is None:
                    return
                try:
                    attempts = 0
                    while not self._stop.is_set() and not seg.complete:
                        before = seg.pos
                        try:
                            self._fetch(seg)
                        except urllib.error.HTTPError as e:
                            if e.code in (401, 403, 404, 410, 416):
                                raise
                            attempts += 1
                            if attempts > MAX_RETRIES:
                                raise
                            self._stop.wait(min(30, 2 ** attempts))
                        except NETWORK_ERRORS:
                            if seg.pos > before:
                                attempts = 0
                            attempts += 1
                            if attempts > MAX_RETRIES:
                                raise
                            self._stop.wait(min(30, 2 ** attempts))
                finally:
                    with self._lock:
                        seg.active = False
        except BaseException as e:  # noqa: BLE001 - report any failure to the UI
            self._fatal = e
            self._stop.set()

    def _assemble(self) -> None:
        self.status = Status.ASSEMBLING
        segs = sorted(self.segments, key=lambda s: s.start)
        expected = 0
        for s in segs:
            if s.start != expected:
                raise IOError("Segments are not contiguous - try re-downloading")
            expected = s.end + 1
        if self.total is not None and expected != self.total:
            raise IOError("Downloaded size does not match the server's file size")

        folder = self.folder
        if self.auto_category:
            folder = os.path.join(folder, category_for(self.filename))
        os.makedirs(folder, exist_ok=True)
        target = unique_path(os.path.join(folder, self.filename or "download"))
        if len(segs) == 1:
            src = self._seg_path(segs[0])
            if os.path.exists(src):
                shutil.move(src, target)
            else:
                open(target, "wb").close()
        else:
            with open(target, "wb") as out:
                for s in segs:
                    with open(self._seg_path(s), "rb") as f:
                        shutil.copyfileobj(f, out, 4 * 1024 * 1024)
        self.final_path = target
        self.filename = os.path.basename(target)
        self.delete_temp()

    def _run(self) -> None:
        try:
            self._probe()
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            self._plan_segments()
            self._sync_from_disk()
            self.status = Status.DOWNLOADING

            workers = [
                threading.Thread(target=self._worker, daemon=True, name=f"dl-{self.id[:6]}-{i}")
                for i in range(self.connections if self.resumable else 1)
            ]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

            if self._fatal is not None:
                raise self._fatal
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            if not all(s.complete for s in self.segments):
                raise IOError("Download stopped before all parts finished")
            self._assemble()
            self.status = Status.COMPLETE
            self.completed_at = time.time()
            self.speed = 0.0
        except urllib.error.HTTPError as e:
            self.status = Status.ERROR
            self.error = f"HTTP {e.code} {e.reason}"
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

    # ---------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        with self._lock:
            segs = [s.to_dict() for s in self.segments]
        status = self.status
        if status in Status.ACTIVE:
            status = Status.QUEUED
        return {
            "kind": self.kind,
            "id": self.id,
            "url": self.url,
            "final_url": self.final_url,
            "folder": self.folder,
            "filename": self.filename,
            "user_filename": self.user_filename,
            "connections": self.connections,
            "headers": self.headers,
            "total": self.total,
            "resumable": self.resumable,
            "segments": segs,
            "status": status,
            "error": self.error,
            "created": self.created,
            "completed_at": self.completed_at,
            "final_path": self.final_path,
            "auto_category": self.auto_category,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Download":
        dl = cls(d["url"], d["folder"], None, d.get("connections", 8), d.get("headers"), d["id"])
        dl.filename = d.get("filename")
        dl.user_filename = d.get("user_filename", False)
        dl.final_url = d.get("final_url") or d["url"]
        dl.total = d.get("total")
        dl.resumable = d.get("resumable", False)
        dl.segments = [Segment(s["start"], s["end"], s.get("done", False)) for s in d.get("segments", [])]
        dl.status = d.get("status", Status.PAUSED)
        dl.error = d.get("error")
        dl.created = d.get("created", time.time())
        dl.completed_at = d.get("completed_at")
        dl.final_path = d.get("final_path")
        dl.auto_category = d.get("auto_category", False)
        if dl.status == Status.COMPLETE:
            with dl._lock:
                for s in dl.segments:
                    s.pos = (s.end or 0) + 1
        return dl
