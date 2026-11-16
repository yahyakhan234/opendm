"""Build OpenDM for Windows (no Python needed to run it).

    python -m venv .venv
    .venv\Scripts\pip install pyinstaller
    .venv\Scripts\python tools/build_exe.py               -> dist/OpenDM.exe (portable, one file)
    .venv\Scripts\python tools/build_exe.py --installer   -> dist/OpenDM-Setup-<version>.exe

The installer needs Inno Setup 6 (winget install JRSoftware.InnoSetup). The installed app
starts faster than the portable exe because it doesn't unpack itself on every launch.

Writes dist/OpenDM.exe with yt-dlp and ffmpeg packed inside, so the person running it
needs nothing else. The copies installed on this PC are used:

    winget install yt-dlp        (also installs yt-dlp's own ffmpeg build, used here)

or pass --ytdlp PATH / --ffmpeg PATH; --no-ytdlp / --no-ffmpeg leave them out.
At runtime OpenDM unpacks them once into its data folder; yt-dlp updates itself daily.

ffmpeg is GPL-licensed: its license and where to get its source go into the zip next to it.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
sys.path.insert(0, ROOT)

from opendm import __version__  # noqa: E402
from opendm.icon import ico_bytes, write_assets  # noqa: E402
from opendm.ytdlp import find_ytdlp  # noqa: E402


def version_file(path: str) -> None:
    """Windows file properties (shown in Explorer > Properties > Details)."""
    nums = tuple(int(x) for x in __version__.split(".")) + (0,) * (4 - len(__version__.split(".")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'OpenDM contributors'),
      StringStruct('FileDescription', 'OpenDM download manager'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'OpenDM'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'OpenDM.exe'),
      StringStruct('ProductName', 'OpenDM'),
      StringStruct('ProductVersion', '{__version__}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")


def stage_ytdlp(path: str | None) -> list[str]:
    """Copy yt-dlp.exe and its version into build/tools; return the PyInstaller arguments."""
    exe = path or find_ytdlp()
    if not exe or not exe.lower().endswith("yt-dlp.exe") or not os.path.isfile(exe):
        raise SystemExit("yt-dlp.exe not found. Install it (winget install yt-dlp), "
                         "pass --ytdlp PATH, or build without it using --no-ytdlp.")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    if not version:
        raise SystemExit(f"{exe} didn't report a version")
    tools = os.path.join(BUILD, "tools")
    os.makedirs(tools, exist_ok=True)
    shutil.copyfile(exe, os.path.join(tools, "yt-dlp.exe"))
    with open(os.path.join(tools, "yt-dlp.version"), "w", encoding="utf-8") as f:
        f.write(version)
    print(f"bundling yt-dlp {version} from {exe}")
    return ["--add-binary", f"{os.path.join(tools, 'yt-dlp.exe')}{os.pathsep}tools",
            "--add-data", f"{os.path.join(tools, 'yt-dlp.version')}{os.pathsep}tools"]


def _installed_ffmpeg() -> str | None:
    """Prefer yt-dlp's own ffmpeg build (it carries fixes yt-dlp relies on)."""
    pkgs = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    for pattern in ("yt-dlp.FFmpeg_*", "Gyan.FFmpeg*", "*FFmpeg*"):
        hits = sorted(glob.glob(os.path.join(pkgs, pattern, "*", "bin", "ffmpeg.exe")))
        if hits:
            return hits[-1]
    return shutil.which("ffmpeg")


FFMPEG_SOURCE_NOTE = """This ffmpeg.exe is a GPL build of FFmpeg (https://ffmpeg.org), shipped unmodified
with OpenDM so videos can be joined and saved as .mp4. OpenDM runs it as a separate program.

FFmpeg source code: https://ffmpeg.org/download.html  (git: https://git.ffmpeg.org/ffmpeg.git)
Build scripts for this build: https://github.com/yt-dlp/FFmpeg-Builds
Version: {version}

Its license (GPL) is in ffmpeg-LICENSE.txt next to this file.
"""


def stage_ffmpeg(path: str | None, packed: bool = True) -> list[str]:
    """Stage ffmpeg.exe + its license in build/tools; return PyInstaller arguments.

    packed=True (portable exe): one LZMA zip, unpacked once at runtime.
    packed=False (installer): the plain files, used straight from the install folder.
    """
    exe = path or _installed_ffmpeg()
    if not exe or not os.path.isfile(exe):
        raise SystemExit("ffmpeg.exe not found. Install it (winget install yt-dlp.FFmpeg), "
                         "pass --ffmpeg PATH, or build without it using --no-ffmpeg.")
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=60).stdout
    first = out.splitlines()[0] if out else ""
    raw = first.split(" ")[2] if first.startswith("ffmpeg version ") else "unknown"
    date = re.search(r"(20\d\d)(\d\d)(\d\d)$", raw)
    # Sortable, so a newer OpenDM.exe replaces an older unpacked copy.
    version = f"{date.group(1)}.{date.group(2)}.{date.group(3)} {raw}" if date else raw
    licence = None
    folder = os.path.dirname(exe)
    for cand in (os.path.join(folder, "..", "LICENSE.txt"), os.path.join(folder, "LICENSE.txt"),
                 os.path.join(folder, "..", "LICENSE")):
        if os.path.isfile(cand):
            licence = cand
            break
    if not licence:
        raise SystemExit(f"No LICENSE file found next to {exe}; it must ship with ffmpeg.")
    tools = os.path.join(BUILD, "tools")
    os.makedirs(tools, exist_ok=True)
    with open(os.path.join(tools, "ffmpeg.version"), "w", encoding="utf-8") as f:
        f.write(version)
    if not packed:
        print(f"bundling ffmpeg {raw} from {exe}")
        shutil.copyfile(exe, os.path.join(tools, "ffmpeg.exe"))
        shutil.copyfile(licence, os.path.join(tools, "ffmpeg-LICENSE.txt"))
        with open(os.path.join(tools, "ffmpeg-SOURCE.txt"), "w", encoding="utf-8") as f:
            f.write(FFMPEG_SOURCE_NOTE.format(version=raw))
        args = ["--add-binary", f"{os.path.join(tools, 'ffmpeg.exe')}{os.pathsep}tools"]
        for name in ("ffmpeg.version", "ffmpeg-LICENSE.txt", "ffmpeg-SOURCE.txt"):
            args += ["--add-data", f"{os.path.join(tools, name)}{os.pathsep}tools"]
        return args
    zpath = os.path.join(tools, "ffmpeg.zip")
    print(f"bundling ffmpeg {raw} from {exe} (compressing, takes a minute)")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_LZMA) as z:
        z.write(exe, "ffmpeg.exe")
        z.write(licence, "LICENSE.txt")
        z.writestr("SOURCE.txt", FFMPEG_SOURCE_NOTE.format(version=raw))
    print(f"  ffmpeg.zip: {os.path.getsize(zpath) / 1e6:.0f} MB")
    return ["--add-data", f"{zpath}{os.pathsep}tools",
            "--add-data", f"{os.path.join(tools, 'ffmpeg.version')}{os.pathsep}tools"]


def find_iscc(given: str | None) -> str:
    cands = [given] if given else []
    cands += [shutil.which("iscc") or ""]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        cands.append(os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe"))
        cands.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("Inno Setup not found. Install it with:  winget install JRSoftware.InnoSetup  "
                     "(or pass --iscc PATH)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build dist/OpenDM.exe, or the installer with --installer")
    ap.add_argument("--installer", action="store_true", help="build dist/OpenDM-Setup-<version>.exe")
    ap.add_argument("--iscc", help="path to Inno Setup's ISCC.exe")
    ap.add_argument("--folder", action="store_true",
                    help="only build the installer's app folder (dist/OpenDM), without Inno Setup")
    ap.add_argument("--ytdlp", help="yt-dlp.exe to bundle (default: the installed one)")
    ap.add_argument("--no-ytdlp", action="store_true", help="don't bundle yt-dlp")
    ap.add_argument("--ffmpeg", help="ffmpeg.exe to bundle (default: the installed one)")
    ap.add_argument("--no-ffmpeg", action="store_true", help="don't bundle ffmpeg")
    opts = ap.parse_args()
    folder = opts.installer or opts.folder
    iscc = find_iscc(opts.iscc) if opts.installer else None  # fail early, before the slow part
    os.makedirs(BUILD, exist_ok=True)
    shutil.rmtree(os.path.join(BUILD, "tools"), ignore_errors=True)
    extra = [] if opts.no_ytdlp else stage_ytdlp(opts.ytdlp)
    extra += [] if opts.no_ffmpeg else stage_ffmpeg(opts.ffmpeg, packed=not folder)
    assets = os.path.join(BUILD, "assets")
    write_assets(assets)  # pre-drawn icons, so the app doesn't draw them on every launch
    extra += ["--add-data", f"{assets}{os.pathsep}assets"]
    ico = os.path.join(BUILD, "opendm.ico")
    with open(ico, "wb") as f:
        f.write(ico_bytes())
    ver = os.path.join(BUILD, "version.txt")
    version_file(ver)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir" if folder else "--onefile", "--windowed", "--name", "OpenDM",
        "--icon", ico, "--version-file", ver, *extra,
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(BUILD, "pyinstaller"),
        "--specpath", BUILD,
        os.path.join(ROOT, "OpenDM.pyw"),
    ], cwd=ROOT)
    shutil.rmtree(os.path.join(BUILD, "pyinstaller"), ignore_errors=True)
    if not folder:
        print("built", os.path.join(ROOT, "dist", "OpenDM.exe"))
        return
    if not opts.installer:
        print("built", os.path.join(ROOT, "dist", "OpenDM"))
        return
    subprocess.check_call([iscc, "/Q", f"/DAppVersion={__version__}", f"/DRoot={ROOT}",
                           os.path.join(ROOT, "installer", "opendm.iss")])
    print("built", os.path.join(ROOT, "dist", f"OpenDM-Setup-{__version__}.exe"))


if __name__ == "__main__":
    main()
