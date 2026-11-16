"""Tools that ship inside OpenDM.exe (yt-dlp and ffmpeg), so nothing else needs installing.

PyInstaller unpacks the exe to a new temp folder on every launch, so each tool is copied
once to <data folder>/tools, where it stays (and where yt-dlp can update itself). A tool
packed as <name>.zip is extracted there; <name>.exe is copied as is. Each has a
<name>.version file next to it, and a newer bundled version replaces an older installed one.

The installed version (a normal folder, not a single exe) doesn't need this for tools
that never change themselves: ffmpeg is used straight from the install folder.

When running from source there is nothing bundled and these functions just report what's
already in the tools folder.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import zipfile

from .util import data_dir

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# Tools that never modify themselves, so they can run from a fixed install folder.
IN_PLACE = {"ffmpeg"}


def _single_file_exe() -> bool:
    """True for the portable OpenDM.exe, which unpacks itself to a new temp folder each launch."""
    return os.path.basename(getattr(sys, "_MEIPASS", "")).startswith("_MEI")


def tools_dir() -> str:
    return os.path.join(data_dir(), "tools")


def _lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def bundled(name: str) -> tuple[str, str] | None:
    """(path, version) of a tool packed inside OpenDM.exe, or None."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    for ext in (".exe", ".zip"):
        path = os.path.join(base, "tools", name + ext)
        if os.path.isfile(path):
            try:
                with open(os.path.join(base, "tools", name + ".version"), encoding="utf-8") as f:
                    version = f.read().strip()
            except OSError:
                version = ""
            return path, version
    return None


def read_state(name: str) -> dict:
    try:
        with open(os.path.join(tools_dir(), name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(name: str, state: dict) -> None:
    try:
        os.makedirs(tools_dir(), exist_ok=True)
        with open(os.path.join(tools_dir(), name + ".json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _unpack(src: str, name: str, dest: str) -> None:
    tmp = dest + ".new"
    if src.endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            with z.open(name + ".exe") as fin, open(tmp, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            for member in z.namelist():  # license files etc.
                if member != name + ".exe" and not member.endswith("/"):
                    with z.open(member) as fin, open(os.path.join(tools_dir(), f"{name}-{os.path.basename(member)}"),
                                                      "wb") as fout:
                        shutil.copyfileobj(fin, fout)
    else:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def install(name: str) -> str | None:
    """Path to OpenDM's own copy of a tool, unpacking the bundled one first if needed."""
    dest = os.path.join(tools_dir(), name + ".exe")
    found = bundled(name)
    if found and name in IN_PLACE and found[0].endswith(".exe") and not _single_file_exe():
        return found[0]  # installed version: use it where it is
    with _lock(name):
        if found:
            src, version = found
            if not os.path.isfile(dest) or (version and version > read_state(name).get("version", "")):
                try:
                    os.makedirs(tools_dir(), exist_ok=True)
                    _unpack(src, name, dest)
                    write_state(name, {"version": version, "checked": 0})
                except (OSError, zipfile.BadZipFile, KeyError):
                    try:
                        os.remove(dest + ".new")
                    except OSError:
                        pass
                    if src.endswith(".exe"):
                        return src  # can't copy (disk full, file in use): run it from the temp folder
    return dest if os.path.isfile(dest) else None


def is_managed(path: str | None) -> bool:
    """True for a tool that came with OpenDM (unpacked to the data folder, or in the install folder)."""
    if not path:
        return False
    folder = os.path.dirname(os.path.abspath(path))
    shipped = os.path.join(getattr(sys, "_MEIPASS", ""), "tools") if getattr(sys, "_MEIPASS", None) else None
    return folder == os.path.abspath(tools_dir()) or (shipped is not None and folder == os.path.abspath(shipped))
