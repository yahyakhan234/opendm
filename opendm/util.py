import os
import re
import sys
import urllib.parse
from email.message import Message

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def data_dir() -> str:
    """Per-user folder for settings, download state and temp segments.

    Set OPENDM_DATA to keep everything somewhere else (e.g. a USB stick for portable use).
    """
    override = os.environ.get("OPENDM_DATA")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, "OpenDM")
    os.makedirs(path, exist_ok=True)
    return path


def default_download_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def sanitize_filename(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name:
        return "download"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[: 200 - len(ext)] + ext
    return name


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    try:
        name = msg.get_filename()
    except Exception:
        name = None
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
        name = m.group(1) if m else None
    return name


def filename_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return name or "download"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


def human_size(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def is_url(text: str) -> bool:
    return bool(re.match(r"^(https?|ftp)://\S+$", text.strip(), re.I))


CATEGORIES = {
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"},
    "Music": {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".opus", ".wma"},
    "Compressed": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Programs": {".exe", ".msi", ".apk", ".dmg", ".deb", ".rpm", ".appimage", ".msix"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".epub", ".csv"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff"},
}


def category_for(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"
